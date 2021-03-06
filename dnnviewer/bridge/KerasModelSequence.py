from . import tensorflow as tf_bridge
from . import ModelError
from .AbstractModelSequence import AbstractModelSequence
from .AbstractActivationMapper import AbstractActivationMapper
from .KerasNetworkExtractor import KerasNetworkExtractor
from ..Grapher import Grapher
from dnnviewer.dataset.DataSet import DataSet
from ..layers.AbstractLayer import AbstractLayer

import tensorflow.keras as keras
import numpy as np
from pathlib import Path
import glob
import re
import logging
import traceback

_logger = logging.getLogger(__name__)


class KerasModelSequence(AbstractModelSequence, AbstractActivationMapper):
    """ Handling a sequence of Keras models, saved as checkpoints or HDF5 """

    def __init__(self, test_data: DataSet):
        AbstractModelSequence.__init__(self)
        self.model_paths = []
        self.current_model = None
        self.test_data = test_data

    # @override
    def reset(self):
        AbstractModelSequence.reset(self)
        self.model_paths = []
        self.current_model = None

    def load_single(self, file_path):
        """" Load a single Keras model from file_path"""

        self.reset()
        self.title = file_path
        self.number_epochs = 1
        self.model_paths = [file_path]

    def load_sequence(self, dir_path):
        """ Load a sequence of models over epochs from dir_path with pattern on {epoch} tag """

        self.reset()
        self.title = dir_path
        checkpoint_glob = dir_path.replace('{epoch}', '[0-9]*')

        checkpoint_path_list = glob.glob(checkpoint_glob)
        checkpoint_epoch_regexp = re.compile(dir_path.replace('{epoch}', '([0-9]*)'))
        checkpoints = {int(checkpoint_epoch_regexp.search(path).group(1)): path for path in checkpoint_path_list}
        checkpoint_epochs = list(checkpoints)
        checkpoint_epochs.sort()
        self.model_paths = [checkpoints[i] for i in checkpoint_epochs]
        self.number_epochs = len(self.model_paths)

    # @override
    def list_models(self, directories, model_sequence_pattern='{model}_{epoch}'):
        """ List all models in directories """
        seq_pat1 = model_sequence_pattern.replace('{model}', '*').replace('{epoch}', '[0-9]*')
        seq_pat2 = model_sequence_pattern.replace('{model}', r'(\w+)').replace('{epoch}', '([0-9]+)')
        models = []

        try:
            for path in directories:
                dir_path = Path(path)

                # HDF5 & TF files
                model_glob_hdf5 = str(dir_path / '*.h5')
                model_path_list = glob.glob(model_glob_hdf5)
                models.extend(model_path_list)
                model_glob_tf = str(dir_path / '*.tf')
                model_path_list = glob.glob(model_glob_tf)
                models.extend(model_path_list)

                # Checkpoints using pattern
                model_glob_seq = str(dir_path / seq_pat1)
                model_path_list = glob.glob(model_glob_seq)
                # Detect unique models
                reg2 = re.compile(seq_pat2)
                seq_model_path_list = [reg2.search(path).group(1) for path in model_path_list]
                model_path_list = [str(dir_path / model_sequence_pattern.replace('{model}', m))
                                   for m in set(seq_model_path_list)]
                models.extend(model_path_list)
        except Exception as e:
            _logger.warning('Failed to list directories')
            _logger.debug(traceback.format_exc(e))
        models.sort()
        return models

    # @override
    def format_test_data(self):
        try:
            in_type, in_shape = self.get_input_geometry()
            self.test_data.x_format = tf_bridge.keras_prepare_input(in_type, in_shape, self.test_data.x)
        except Exception as e:
            raise ModelError("Error while formatting test data: %s" % str(e))

    # @override
    def setup_generator(self, generator_builder):
        if generator_builder:
            in_type, in_shape = self.get_input_geometry()
            self.test_data.mode = DataSet.MODE_GENERATOR
            self.test_data.generator = generator_builder(in_type, in_shape)

    # @override
    def get_input_geometry(self):
        """ Return the type and shape of the model input """
        if self.number_epochs == 0:
            raise ModelError("No model available")

        # Take as reference the first model of the sequence
        model = self._load_keras_model(0)
        return model.input.dtype.as_numpy_dtype, model.input.shape.as_list()

    # @override
    def get_activation(self, img, layer: AbstractLayer, unit=None):

        # Expand dimension to create a mini-batch of 1 element
        batch = np.expand_dims(img, 0)

        # Create partial model
        keras_layer, access_layers = self._get_keras_layer(self.current_model, layer.name, layer.path)
        if not keras_layer:
            return None

        try:
            intermediate_model = keras.models.Model(inputs=self.current_model.get_input_at(0),
                                                    outputs=keras_layer.get_output_at(len(access_layers) - 1))
            maps = intermediate_model.predict(batch)[0]

        except ValueError as e:
            _logger.error('Fail to predict from input to partial output: %s', str(e))
            return None

        if unit is None:
            return maps
        else:
            return maps[:, :, unit]

    def _load_keras_model(self, model_index):
        """ Return requested Keras model from sequence """
        model_path = Path(self.model_paths[model_index])

        if not model_path.exists():
            raise ModelError("Model path not found '%s'" % str(model_path))

        return keras.models.load_model(str(model_path))

    def _load_model(self, grapher: Grapher, model_index: int):

        self.current_model = self._load_keras_model(model_index)

        # Top level properties of the DNN model
        tf_bridge.keras_set_model_properties(grapher, self.current_model)

        # Create all other layers from the Keras Sequential model
        extractor = KerasNetworkExtractor(grapher, self.current_model, self.test_data)
        extractor.process()

        self.current_epoch_index = model_index
        return self.current_epoch_index

    def _get_keras_layer(self, model, name: str, path: str):
        segments = path.split('/')
        cur_layer = model
        layers = [model]
        for segment in filter(lambda s: s, segments):
            try:
                cur_layer = cur_layer.get_layer(name=segment)
                layers.append(cur_layer)
            except ValueError:
                _logger.error('Fail to find layer path segment %s within path %s', segment, path)
                return None

        try:
            cur_layer = cur_layer.get_layer(name=name)
        except ValueError:
            _logger.error('Fail to find layer %s within path %s', name, path)
            return None

        return cur_layer, layers
