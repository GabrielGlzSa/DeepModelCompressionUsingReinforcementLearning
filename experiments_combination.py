import os
import pandas as pd
import tensorflow as tf
import tensorflow.keras.backend as K
import tensorflow_datasets as tfds
import logging
import numpy as np
from CompressionTechniques import ModelCompression
import CompressionTechniques
import importlib
import inspect

# 0 = all messages are logged (default behavior)
# 1 = INFO messages are not printed
# 2 = INFO and WARNING messages are not printed
# 3 = INFO, WARNING, and ERROR messages are not printed


os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2'

tf.get_logger().setLevel('ERROR')
# logging.root.setLevel(logging.INFO)

logging.basicConfig(level=logging.INFO, format='%(asctime)s -%(levelname)s - %(funcName)s -  %(message)s')
logger = logging.getLogger(__name__)

tfds.disable_progress_bar()

# horses_or_humans
# mnist
# cifar10
# imagenet2021
# fashion_mnist


dataset_name = 'horses_or_humans'

splits, info = tfds.load(dataset_name, as_supervised=True, with_info=True,
                         split=['train[:80%]', 'train[80%:]', 'test'], data_dir='./data')

(train_examples, validation_examples, test_examples) = splits

print(info)
num_examples = info.splits['train'].num_examples
num_classes = info.features['label'].num_classes
input_shape = info.features['image'].shape
BATCH_SIZE = 32
IMAGE_SIZE = None
input_shape = list(input_shape)
print(input_shape)
if input_shape[0]>224:
    IMAGE_SIZE = 224
else:
    IMAGE_SIZE = input_shape[0]

input_shape[0] = IMAGE_SIZE
input_shape[1] = IMAGE_SIZE

print('Number of examples', num_examples)
print('Number of classes', num_classes)


@tf.function
def map_fn(img, label):
    img = tf.image.resize(img, size=(IMAGE_SIZE, IMAGE_SIZE))
    img /= 255.

    return img, label


def prepare_dataset(train_examples, validation_examples, test_examples, num_examples, map_fn, batch_size):
    train_ds = train_examples.shuffle(buffer_size=num_examples).map(map_fn).batch(batch_size)
    valid_ds = validation_examples.map(map_fn).batch(batch_size)
    test_ds = test_examples.map(map_fn).batch(batch_size)

    return train_ds, valid_ds, test_ds


train_ds, valid_ds, test_ds = prepare_dataset(train_examples, validation_examples, test_examples, num_examples, map_fn,
                                              BATCH_SIZE)

epochs = 30

def create_model(optimizer, loss_object, train_metric):
    path = './data/saved_models/{}/'.format(dataset_name)

    model = tf.keras.Sequential([tf.keras.layers.Conv2D(32, (3, 3), activation='relu', name='conv2d_0',
                                                        input_shape=input_shape),
                                 tf.keras.layers.Conv2D(32, (3, 3), activation='relu', name='conv2d_1'),
                                 tf.keras.layers.MaxPool2D((2, 2), 2),
                                 tf.keras.layers.Flatten(),
                                 tf.keras.layers.Dense(128, activation='relu', name='dense_0'),
                                 tf.keras.layers.Dense(128, activation='relu', name='dense_1'),
                                 tf.keras.layers.Dense(num_classes, activation='softmax', name='dense_softmax')
                                 ])
    model.compile(optimizer=optimizer, loss=loss_object, metrics=train_metric)

    try:
        model.build(input_shape=input_shape)
        model.load_weights(path)

        logger.info('Loaded model successfully.')
    except:
        logger.error('Model not found. Training a new model.')
        cp_callback = tf.keras.callbacks.ModelCheckpoint(
            filepath=path,
            verbose=1,
            save_best_only=True,
            save_weights_only=True)

        model.fit(train_ds, epochs=epochs, validation_data=valid_ds, callbacks=[cp_callback])
    return model


def test_compression(df_row, params):
    tf.keras.backend.clear_session()
    logger.info('Using compressors: {} & {}.'.format(df_row['conv_compr'],
                                                     df_row['dense_compr']))

    optimizer = tf.keras.optimizers.Adam()
    loss_object = tf.keras.losses.SparseCategoricalCrossentropy()
    train_metric = tf.keras.metrics.SparseCategoricalAccuracy()
    # First time to fit in case model is missing.
    model = create_model(optimizer, loss_object, train_metric)
    print(model.summary())
    # # Load the model from disk as to avoid bug of getting different results.
    # model = create_model(optimizer, loss_object, train_metric)
    loss, train_acc_b = model.evaluate(train_ds)
    logger.info('Loss={} and accuracy={} for train_ds using original model.'.format(loss, train_acc_b))
    loss, val_acc_b = model.evaluate(valid_ds)
    logger.info('Loss={} and accuracy={} for valid_ds using original model.'.format(loss, val_acc_b))
    loss, test_acc_b = model.evaluate(test_ds)
    logger.info('Loss={} and accuracy={} for test_ds using original model.'.format(loss, test_acc_b))

    class_ = getattr(CompressionTechniques, df_row['conv_compr'])
    optimizer = tf.keras.optimizers.Adam()
    loss_object = tf.keras.losses.SparseCategoricalCrossentropy()
    train_metric = tf.keras.metrics.SparseCategoricalAccuracy()
    compressor = class_(model=model, dataset=train_ds, optimizer=optimizer, loss=loss_object, metrics=train_metric,
                        fine_tune=True)

    if params[df_row['conv_compr']]:
        compressor.compress_layer(**params[df_row['conv_compr']])
    else:
        compressor.compress_layer()

    model = compressor.get_model()

    print(model.summary())
    weights_before1, weights_after1 = compressor.get_weights_diff()

    class_ = getattr(CompressionTechniques, df_row['dense_compr'])
    optimizer = tf.keras.optimizers.Adam()
    loss_object = tf.keras.losses.SparseCategoricalCrossentropy()
    train_metric = tf.keras.metrics.SparseCategoricalAccuracy()
    compressor = class_(model=model, dataset=train_ds, optimizer=optimizer, loss=loss_object, metrics=train_metric,
                        fine_tune=True)
    # Replace the initialized number of weights as it might be different due to special layers.
    compressor.weights_before = weights_after1
    if params[df_row['dense_compr']]:
        compressor.compress_layer(**params[df_row['dense_compr']])
    else:
        compressor.compress_layer()

    model = compressor.get_model()
    weights_before2 , weights_after2 = compressor.get_weights_diff()
    print(model.summary())

    logger.info('weights before & after: {}->{}={}->{}'.format(weights_before1,
                                                                weights_after1,
                                                                weights_before2,
                                                                weights_after2))


    loss, train_acc_a = model.evaluate(train_ds)
    logger.info('Loss={} and accuracy={} for train_ds using optimized model.'.format(loss, train_acc_a))
    loss, val_acc_a = model.evaluate(valid_ds)
    logger.info('Loss={} and accuracy={} for valid_ds using optimized model.'.format(loss, val_acc_a))
    loss, test_acc_a = model.evaluate(test_ds)
    logger.info('Loss={} and accuracy={} for test_ds using optimized model.'.format(loss, test_acc_a))
    labels = ['train', 'val', 'test']
    labels = list(map(lambda label: label + '_acc', labels))
    labels = list(map(lambda label: label + '_ori', labels)) + list(map(lambda label: label + '_new', labels))
    labels.extend(['weights_before', 'weights_after'])
    logger.info('Original model has {} trainable weights.'.format(weights_before1))
    logger.info('Compressed model has {} trainable weights.'.format(weights_after2))
    return dict(zip(labels,
                    [train_acc_b, val_acc_b, test_acc_b,
                     train_acc_a, val_acc_a, test_acc_a,
                     weights_before1, weights_after2]))


parameters = {}

compressors = [name for name, cls in
               inspect.getmembers(importlib.import_module("CompressionTechniques"), inspect.isclass) if
               issubclass(cls, ModelCompression)]

print(compressors)
optimizer = tf.keras.optimizers.Adam()
loss_object = tf.keras.losses.SparseCategoricalCrossentropy()
train_metric = tf.keras.metrics.SparseCategoricalAccuracy()
# First time to fit in case model is missing.
model = create_model(optimizer, loss_object, train_metric)

conv_compressors = []
dense_compressors = []
for compressor in compressors:
    if compressor in ['MLPCompression', 'SparseConvolutionCompression']:
        continue
    class_ = getattr(CompressionTechniques, compressor)
    temp_comp = class_(model=model, dataset=train_ds, optimizer=optimizer, loss=loss_object, metrics=train_metric,
                        fine_tune=True)
    if temp_comp.target_layer_type == 'conv':
        conv_compressors.append(compressor)
    elif temp_comp.target_layer_type == 'dense':
        dense_compressors.append(compressor)

del model
del optimizer
del train_metric

print(conv_compressors)
print(dense_compressors)

try:
    df = pd.read_csv('./data/results_combi2_{}.csv'.format(dataset_name))
except:
    df = pd.DataFrame()
    for conv in conv_compressors:
        for dense in dense_compressors:
            row = pd.Series(data={'conv_compr':conv,
                                         'dense_compr':dense},
                                   index=['conv_compr', 'dense_compr'],
                                   copy=True)

            df = df.append(row, ignore_index=True)
    df['completed'] = False
    df = df.reset_index(drop=True)

parameters['DeepCompression'] = {'layer_name': 'dense_0', 'threshold': 0.001}
parameters['ReplaceDenseWithGlobalAvgPool'] = {'layer_name': 'dense_1'}
parameters['InsertDenseSVD'] = {'layer_name': 'dense_0', 'units': 16}
parameters['InsertDenseSVDCustom'] = {'layer_name': 'dense_0', 'units': 16}
parameters['InsertDenseSparse'] = {'layer_name': 'dense_0', 'verbose': True, 'units':16}
parameters['InsertSVDConv'] = {'layer_name': 'conv2d_1', 'units': 8}
parameters['DepthwiseSeparableConvolution'] = {'layer_name': 'conv2d_1'}
parameters['FireLayerCompression'] = {'layer_name': 'conv2d_1'}
parameters['MLPCompression'] = {'layer_name': 'conv2d_1'}
parameters['SparseConnectionsCompression'] = {'layer_name': 'conv2d_1', 'epochs':20,
                                              'target_perc':0.75, 'conn_perc_per_epoch':0.1}

print(df.to_string())

banned = [1,2,
          6,7,
          11, 12,
          16,17]

size = df.shape[0]
for idx in range(size):
    if df.at[idx, 'completed'] or idx in banned:
        logger.info('Skipping {}->{} since it was completed before.'.format(df.at[idx, 'conv_compr'],
                                                                            df.at[idx, 'dense_compr']))
        continue
    resultados = test_compression(df.iloc[idx], parameters)
    for key, value in resultados.items():
        df.at[idx, key] = value
    df.at[idx, 'completed'] = True
    print(df.to_string())
    df.to_csv('./data/results_combi2_{}.csv'.format(dataset_name), index=False)