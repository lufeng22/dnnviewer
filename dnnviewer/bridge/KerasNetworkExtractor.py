from . import ModelError
from .tensorflow import get_caption, keras_prepare_labels
from ..Grapher import Grapher
from ..layers.Input import Input
from ..layers.Dense import Dense
from ..layers.Convo2D import Convo2D
from ..TestData import TestData
from ..theming.Theme import float_fmt

from tensorflow import keras
import tensorflow as tf
import numpy as np
import logging

_logger = logging.getLogger(__name__)


class KerasNetworkExtractor:

    def __init__(self, grapher: Grapher, model: keras.models.Model, test_data: TestData):
        self.grapher = grapher
        self.model = model
        self.test_data = test_data

    def process(self):
        """ Create a graphical representation of a Keras Sequential model's layers
            Compute gradients from test samples
        """

        if not isinstance(self.model, keras.models.Sequential):
            raise ModelError("Unexpected model of type '%s', expecting Sequential model" % type(self.model))

        if len(self.model.layers) == 0:
            _logger.error("Empty model")
            return

        theme = self.grapher.theme

        previous_layer = None
        layer_training_props, layer_input_props = {}, {}
        self.grapher.reset()

        # Get Gradients if test data is available
        grads = self.compute_gradients()

        # Input placeholder if first layer is a layer with weights
        if len(self.model.layers[0].get_weights()) > 0:
            input_dim = self.model.layers[0].get_weights()[0].shape[-2]
            input_layer = Input('input', input_dim, theme, self.test_data.input_classes)
            input_layer.output_props['shape'] = self.model.layers[0].input_shape[1:]
            self.grapher.add_layer(input_layer)
            previous_layer = input_layer

        idx_grads = 0
        for keras_layer in self.model.layers:
            next_layer_input_props = {}
            next_layer_training_props = {}

            layer_class = type(keras_layer).__name__

            if layer_class == 'Dense':
                layer = Dense(keras_layer.name, keras_layer.output_shape[-1],
                              keras_layer.weights[0].numpy(), grads[idx_grads].numpy() if grads else None,
                              theme)

                self._process_add_layer(layer, keras_layer, layer_training_props, layer_input_props)

                previous_layer = layer

            elif layer_class == 'Conv2D':
                layer = Convo2D(keras_layer.name, keras_layer.output_shape[-1],
                                keras_layer.weights[0].numpy(), grads[idx_grads].numpy() if grads else None,
                                theme)

                self._process_add_layer(layer, keras_layer, layer_training_props, layer_input_props)

                # Extra properties
                strides = KerasNetworkExtractor._get_keras_layer_attribute(keras_layer, 'strides')
                if strides and keras_layer.strides != (1, 1):
                    layer.structure_props['strides'] = str(keras_layer.strides)
                    layer.append_sampling_factor([strides[0], strides[1], 1, 1])
                padding = KerasNetworkExtractor._get_keras_layer_attribute(keras_layer, 'padding')
                if padding:
                    layer.structure_props['padding'] = padding

                previous_layer = layer

            elif layer_class == 'Flatten':
                if isinstance(previous_layer, Convo2D):
                    previous_layer.flatten_output = True
                    # Update property on output shape
                    previous_layer.output_props['shape'] = "%s (flat)" % keras_layer.output_shape[1:]
                elif previous_layer is None:
                    # Input layer
                    input_dim = keras_layer.get_output_shape_at(0)[-1]
                    layer = Input('input', input_dim, theme, self.test_data.input_classes)
                    layer.output_props['shape'] = keras_layer.output_shape[1:]
                    self.grapher.add_layer(layer)
                    previous_layer = layer

            # Pooling layers
            elif layer_class in _pooling_layers:
                if layer_class in _pooling_captions:
                    pool_size = KerasNetworkExtractor._get_keras_layer_attribute(keras_layer, 'pool_size')
                    if pool_size is None:
                        size = np.array(keras_layer.input_shape[1:-1]) / np.array(keras_layer.output_shape[1:-1])
                        pool_size = tuple(size)
                    previous_layer.output_props['pooling'] = str(pool_size)
                    previous_layer.append_sampling_factor(1 / np.array(pool_size))
                    # Update output shape on previous layer to take into account for the pooling
                    previous_layer.output_props['shape'] = keras_layer.output_shape[1:]
                else:
                    _logger.error('Unsupported pooling layer: %s', layer_class)

            # Activation standalone layer => add to previous layer
            elif layer_class == 'Activation':
                if previous_layer is not None:
                    # Note: if previous layer already has an activation that is not "linear",
                    #   this property is overridden
                    activation = KerasNetworkExtractor._get_keras_layer_attribute(keras_layer, 'activation',
                                                                                  look_in_config=True)
                    previous_layer.structure_props['activation'] = get_caption(activation, _activation_captions)
                else:
                    # Unusual case in which first layer is activation => report ignored
                    _logger.error('Unsupported activation layer as first layer')

            # Dropout layers
            elif layer_class in _dropout_layers:
                if layer_class in _dropout_captions:
                    rate = KerasNetworkExtractor._get_keras_layer_attribute(keras_layer, 'rate', look_in_config=True)
                    if rate is None:
                        rate = '-'
                    next_layer_training_props['dropout'] = "%s (%s)" % \
                                                           (_dropout_captions[layer_class], rate)
                else:
                    _logger.error('Unsupported dropout layer: %s', layer_class)

            # Batch norm
            elif layer_class == 'BatchNormalization':
                next_layer_input_props['batch_normalization'] = 'Enabled'

            # Ignored
            elif layer_class in _keras_ignored_layers:
                _logger.info('Ignored %s', keras_layer.name)

            else:
                _logger.error('Not handled layer %s of type %s' % (keras_layer.name, type(keras_layer)))

            idx_grads += len(keras_layer.trainable_weights)
            layer_training_props = next_layer_training_props
            layer_input_props = next_layer_input_props

        if self.test_data.output_classes is not None:
            self.grapher.layers[-1].unit_names = self.test_data.output_classes

    def _process_add_layer(self, layer, keras_layer, layer_training_props, layer_input_props):
        """ Add a layer to the model and set existing input and structure properties
            - Add regularization and bias if any
        """
        # Previously set properties
        layer.training_props.update(layer_training_props)
        layer.input_props.update(layer_input_props)

        # Input-Output shape properties
        layer.input_props['shape'] = keras_layer.input_shape[1:]
        layer.output_props['shape'] = keras_layer.output_shape[1:]

        # Regularizers
        for reg_attr in ['activity_regularizer', 'bias_regularizer', 'kernel_regularizer']:
            attrs = ['l1', 'l2']
            reg = KerasNetworkExtractor._get_keras_layer_attribute(keras_layer, reg_attr, attrs, look_in_config=True)
            value = None
            if reg is not None:

                if reg['l1']:
                    if reg['l2']:
                        value = _regularizers_captions['l1_l2'] % (reg['l1'], reg['l2'])
                    else:
                        value = _regularizers_captions['l1'] % reg['l1']
                elif reg['l2']:
                    value = _regularizers_captions['l2'] % reg['l2']
            if value:
                layer.training_props[reg_attr] = value

        # Bias
        if (not hasattr(keras_layer, 'use_bias') or keras_layer.use_bias) and keras_layer.bias is not None:
            layer.bias = keras_layer.bias.numpy()

        # Activation
        activation = KerasNetworkExtractor._get_keras_layer_attribute(keras_layer, 'activation', look_in_config=True)
        if activation is not None:
            layer.structure_props['activation'] = get_caption(activation, _activation_captions)

        self.grapher.add_layer(layer)

    def compute_gradients(self):
        """ Compute gradients applying a mini-batch of test data """
        try:
            if self.test_data.has_test_sample:
                with tf.GradientTape() as tape:
                    n_grad_samples = 128
                    y_est = self.model(self.test_data.x_format[:n_grad_samples])
                    labels = keras_prepare_labels(self.model, self.test_data.y[:n_grad_samples])
                    objective = self.model.loss_functions[0](labels, y_est)
                    return tape.gradient(objective, self.model.trainable_variables)
            else:
                return None

        except Exception as e:
            _logger.error('Unable to compute gradients for model %s: %s', self.model.name, str(e))
            return None

    @staticmethod
    def _get_keras_layer_attribute(keras_layer, attribute: str, sub_attr=None, look_in_config=False):
        """
        Safely get the value of an attribute from a Keras layer
        - If sub_attr is set, will return a dictionary with the listed sub attributes
        - If look_in_metadata is set, will also look in the layer metadata
        """
        value = None
        ret_attr = {}
        try:
            value = getattr(keras_layer, attribute)
        except AttributeError:
            pass

        if value is not None:
            if sub_attr is not None:

                # Return a dictionary with only the required sub-attributes
                for sa in sub_attr:
                    try:
                        ret_attr[sa] = getattr(value, sa)
                    except AttributeError:
                        pass
                return ret_attr

        # Could not find the attribute
        # For some reason, saved models sometimes are squeezing some attributes but keep them in the metadata
        if look_in_config:
            config = keras_layer.get_config()
            if config is not None and attribute in config.keys():
                if isinstance(config[attribute], dict) and 'config' in config[attribute].keys():
                    value = config[attribute]['config']
                elif isinstance(config[attribute], (str, int, float)):
                    value = config[attribute]

        if value is not None:
            if sub_attr is not None:

                # Return only requested sub-attributes as dictionary
                # Return a dictionary with only the required sub-attributes
                for sa in sub_attr:
                    try:
                        ret_attr[sa] = value[sa]
                    except AttributeError:
                        pass
                return ret_attr

        return value


# Layer types
_pooling_layers = {
    'MaxPooling1D', 'MaxPooling2D', 'MaxPooling3D',
    'AveragePooling1D', 'AveragePooling2D', 'AveragePooling3D',
    'GlobalAveragePooling1D', 'GlobalAveragePooling2D', 'GlobalAveragePooling3D',
    'GlobalMaxPooling1D', 'GlobalMaxPooling2D', 'GlobalMaxPooling3D',
}

_dropout_layers = ['Dropout',
                   'SpatialDropout1D', 'SpatialDropout2D', 'SpatialDropout3D']

_keras_ignored_layers = ['ActivityRegularization']

# Captions
_pooling_captions = {
    'MaxPooling1D': 'Max 1D',
    'MaxPooling2D': 'Max 2D',
    'MaxPooling3D': 'Max 3D',
    'AveragePooling1D': 'Average 1D',
    'AveragePooling2D': 'Average 2D',
    'AveragePooling3D': 'Average 3D',
    'GlobalAveragePooling1D': 'Global average 1D',
    'GlobalAveragePooling2D': 'Global average 2D',
    'GlobalAveragePooling3D': 'Global average 3D',
    'GlobalMaxPooling1D': 'Global max 1D',
    'GlobalMaxPooling2D': 'Global max 2D',
    'GlobalMaxPooling3D': 'Global max 3D'
}

_dropout_captions = {'Dropout': 'Standard',
                     'SpatialDropout1D': 'Spatial 1D',
                     'SpatialDropout2D': 'Spatial 2D',
                     'SpatialDropout3D': 'Spatial 3D'}

_activation_captions = {'elu': 'Exponential linear unit',
                        'exponential': 'Exponential',
                        'hard_sigmoid': 'Hard sigmoid',
                        'linear': 'Linear',
                        'relu': 'Rectified linear unit',
                        'selu': 'Scaled Exponential Linear Unit (SELU)',
                        'sigmoid': 'Sigmoid', 'softmax': 'Soft-max',
                        'softplus': 'Soft-plus',
                        'softsign': 'Soft-sign',
                        'swish': 'Swish',
                        'tanh': 'Hyperbolic tangent'}

_regularizers_captions = {'l1': 'L1 - Lasso (%s)' % float_fmt,
                          'l2': 'L2 - Ridge (%s)' % float_fmt,
                          'l1_l2': 'L1-L2 - Elasticnet (%s, %s)' % (float_fmt, float_fmt)}
