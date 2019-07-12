import os
import json
import datetime
import pickle

import torch

from utils.util import ensure_dir, get_instance
from utils.visualization import WriterTensorboardX
from utils.logging_config import logger
import data_loader.data_loaders as module_data
import model.loss as module_loss
import model.metric as module_metric
import model.model as module_arch
from .training_pipeline import TrainingPipeline
from .testing_pipeline import TestingPipeline


class PipelineManager():
    """
    Training pipeline manager that controls train/validation process
    """
    def __init__(self, args, config):
        self.start_time = datetime.datetime.now().strftime('%m%d_%H%M%S')

        self.args = args
        self.config = config

        self.model = None
        self.data_loader = None
        self.valid_data_loaders = []
        self.loss_functions = None
        self.evaluation_metrics = None
        self.optimizer = None
        self.lr_scheduler = None

        # _setup_pipeline() will intialize the above attribute if needed, based on the config
        self.pipeline = self._setup_pipeline()

    def _setup_device(self):
        def prepare_device(n_gpu_use):
            """
            setup GPU device if available, move model into configured device
            """
            n_gpu = torch.cuda.device_count()
            if n_gpu_use > 0 and n_gpu == 0:
                logger.warning(
                    "Warning: There\'s no GPU available on this machine, training will be performed on CPU.")
                n_gpu_use = 0
            if n_gpu_use > n_gpu:
                msg = (f"Warning: The number of GPU\'s configured to use is {n_gpu_use}"
                       f"but only {n_gpu} are available on this machine.")
                logger.warning(msg)
                n_gpu_use = n_gpu
            device = torch.device('cuda:0' if n_gpu_use > 0 else 'cpu')
            list_ids = list(range(n_gpu_use))
            return device, list_ids
        self.device, self.device_ids = prepare_device(self.config['n_gpu'])

    def _setup_model(self):
        model = get_instance(
            module_arch, 'arch', self.config,
        )
        # Print out the model architecture and number of parameters
        model.summary()
        self.model = model.to(self.device)

        if self.args.pretrained is not None:
            logger.info("Loading pretrained checkpoint: {} ...".format(self.args.pretrained))
            checkpoint = torch.load(self.args.pretrained)
            self.model.load_state_dict(checkpoint['state_dict'], strict=False)

        if self.args.resume:
            self._resume_checkpoint(self.args.resume)

        if len(self.device_ids) > 1:
            self.model = torch.nn.DataParallel(model, device_ids=self.device_ids)

    def _resume_checkpoint(self, resume_path):
        """
        Resume from saved checkpoints

        :param resume_path: Checkpoint path to be resumed
        """
        logger.info("Loading checkpoint: {} ...".format(resume_path))
        checkpoint = torch.load(resume_path)
        self.start_epoch = checkpoint['epoch'] + 1
        self.monitor_best = checkpoint['monitor_best']

        # Estimated iteration_count is based on length of the current data loader,
        # which will be wrong if the batch sizes between the two training processes are different.
        self.train_iteration_count = checkpoint.get(
            'train_iteration_count',
            (checkpoint['epoch'] - 1) * len(self.data_loader))
        self.valid_iteration_counts = checkpoint.get(
            'valid_iteration_counts', [
                (checkpoint['epoch'] - 1) * len(self.valid_data_loaders[i])
                for i in range(len(self.valid_data_loaders))])
        self.valid_iteration_counts = list(self.valid_iteration_counts)

        # load architecture params from checkpoint.
        if checkpoint['config']['arch'] != self.config['arch']:
            logger.warning(
                'Warning: Architecture configuration given in config file is different from that of checkpoint. '
                'This may yield an exception while state_dict is being loaded.'
            )
        self.model.load_state_dict(checkpoint['state_dict'])

        # load optimizer state from checkpoint only when optimizer type is not changed.
        if checkpoint['config']['optimizer']['type'] != self.config['optimizer']['type']:
            logger.warning('Warning: Optimizer type given in config file is different from that of checkpoint. '
                                'Optimizer parameters not being resumed.')
        else:
            self.optimizer.load_state_dict(checkpoint['optimizer'])

        self.train_logger = checkpoint['logger']
        logger.info("Checkpoint '{}' (epoch {}) loaded".format(resume_path, self.start_epoch))

    def _setup_data_loader(self):
        self.data_loader = get_instance(module_data, 'data_loader', self.config)

    def _setup_valid_data_loaders(self):
        if 'valid_data_loaders' in self.config.keys():
            self.valid_data_loaders = [
                getattr(module_data, entry['type'])(**entry['args'])
                for entry in self.config['valid_data_loaders']
            ]
        else:
            self.valid_data_loaders = [self.data_loader.split_validation()]

    def _setup_loss_functions(self):
        self.loss_functions = {
            entry.get('nickname', entry['type']): (
                getattr(module_loss, entry['type'])(**entry['args']),
                entry['weight']
            )
            for entry in self.config['losses']
        }

    def _setup_evaluation_metrics(self):
        self.evaluation_metrics = [
            getattr(module_metric, entry['type'])(**entry['args'])
            for entry in self.config['metrics']
        ]

    def _setup_optimizer(self):
        trainable_params = filter(lambda p: p.requires_grad, self.model.parameters())
        self.optimizer = get_instance(torch.optim, 'optimizer', self.config, trainable_params)

    def _setup_lr_scheduler(self):
        self.lr_scheduler = get_instance(torch.optim.lr_scheduler, 'lr_scheduler', self.config, self.optimizer)

    def _setup_checkpoint_dir(self):
        self.checkpoint_dir = os.path.join(self.config['trainer']['save_dir'], self.config['name'], self.start_time)
        # Save configuration file into checkpoint directory:

        ensure_dir(self.checkpoint_dir)
        config_save_path = os.path.join(self.checkpoint_dir, 'config.json')
        with open(config_save_path, 'w') as handle:
            json.dump(self.config, handle, indent=4, sort_keys=False)

    def _setup_writer(self):
        # setup visualization writer instance
        writer_dir = os.path.join(self.config['visualization']['log_dir'], self.config['name'], self.start_time)
        self.writer = WriterTensorboardX(writer_dir, logger, self.config['visualization']['tensorboardX'])

    def _create_training_pipeline(self):
        training_pipeline = TrainingPipeline(
            self.model, self.data_loader, self.config,
            losses=self.loss_functions, metrics=self.evaluation_metrics, optimizer=self.optimizer,
            writer=self.writer, checkpoint_dir=self.checkpoint_dir,
            valid_data_loaders=self.valid_data_loaders, lr_scheduler=self.lr_scheduler,
            **self.config['trainer_args']
        )

        if self.args.pretrained is not None:
            training_pipeline.load_pretrained(self.args.pretrained)
        return training_pipeline

    def _create_testing_pipeline(self):
        # this line is to solve the error described in https://github.com/pytorch/pytorch/issues/973
        torch.multiprocessing.set_sharing_strategy('file_system')
        saved_keys = ['verb_logits', 'noun_logits', 'uid', 'verb_class', 'noun_class']
        for loader in self.valid_data_loaders:
            file_path = os.path.join(self.args.save_dir, loader.name + '.pkl')
            if os.path.exists(file_path) and self.args.skip_exists:
                logger.warning(f'Skipping inference and saving {file_path}')
                continue
            inference_results = trainer.inference(loader, saved_keys)
            with open(file_path, 'wb') as f:
                logger.info(f'Saving results on loader {loader.name} into {file_path}')
                pickle.dump(inference_results, f)

    def _setup_pipeline(self):
        self._setup_device()
        self._setup_model()
        self._setup_data_loader()
        self._setup_checkpoint_dir()
        self._setup_writer()

        if self.args.mode == 'train':
            self._setup_valid_data_loaders()
            self._setup_loss_functions()
            self._setup_optimizer()
            self._setup_lr_scheduler()
            self.pipeline = self._create_training_pipeline()
        else:
            self.pipeline = self._create_testing_pipeline()

        self._setup_evaluation_metrics()

    def run(self):
        self.pipeline.run()
