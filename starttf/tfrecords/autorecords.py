import os
import numpy as np
from multiprocessing import Pool
import tensorflow as tf
from os import listdir
from os.path import isfile, join
import json

PHASE_TRAIN = "train"
PHASE_VALIDATION = "validation"


def _bytes_feature(value):
    return tf.train.Feature(bytes_list=tf.train.BytesList(value=[value]))


def _write_tf_record_pool_helper(args):
    data, num_threads, i, record_filename, preprocess_feature, preprocess_label = args
    data_fn, data_params = data
    thread_name = "%s:thread_%d" % (record_filename, i)
    _write_tf_record(data_fn(data_params, num_threads, i), record_filename, preprocess_feature, preprocess_label, thread_name=thread_name)


def _write_tf_record(data, record_filename, preprocess_feature=None, preprocess_label=None, thread_name="thread"):
    writer = tf.python_io.TFRecordWriter(record_filename)

    samples_written = 0
    for feature, label in data:
        if preprocess_feature is not None:
            feature = preprocess_feature(feature)
        if preprocess_label is not None:
            label = preprocess_label(feature, label)

        feature_dict = {}

        for k in feature.keys():
            feature_dict['feature_' + k] = _bytes_feature(np.reshape(feature[k], (-1,)).tobytes())
        for k in label.keys():
            feature_dict['label_' + k] = _bytes_feature(np.reshape(label[k], (-1,)).tobytes())

        example = tf.train.Example(features=tf.train.Features(
            feature=feature_dict))
        writer.write(example.SerializeToString())
        samples_written += 1
        if samples_written % 10000 == 0:
            print("Samples written by %s: %d." % (thread_name, samples_written))
    print("Samples written by %s: %d." % (thread_name, samples_written))
    writer.close()


def _read_tf_record(record_filename, config):
    reader = tf.TFRecordReader()
    _, serialized_example = reader.read(record_filename)

    feature_dict = {}
    for k in config.keys():
        if "feature_" in k or "label_" in k:
            feature_dict[k] = tf.FixedLenFeature([], tf.string)

    data = tf.parse_single_example(
        serialized_example,
        features=feature_dict)

    outputs = {}
    for k in feature_dict.keys():
        feature_shape = config[k]["shape"]
        feature_type = np.dtype(config[k]["dtype"])
        feature = tf.decode_raw(data[k], feature_type)
        feature_len = 1
        for x in list(feature_shape):
            feature_len *= x
        feature.set_shape((feature_len,))
        outputs[k] = feature

    return outputs


def write_data(prefix,
               threadable_generator,
               params,
               num_threads,
               preprocess_feature=None,
               preprocess_label=None):
    data_tmp_folder = "/".join(prefix.split("/")[:-1])
    if not os.path.exists(data_tmp_folder):
        os.makedirs(data_tmp_folder)

    args = [((threadable_generator, params), num_threads, i, (prefix + "_%d.tfrecords") % i,
                   preprocess_feature, preprocess_label) for i in range(num_threads)]

    # Retrieve a single sample
    data_gen = threadable_generator(params)
    sample_feature, sample_label = next(data_gen)

    # Preprocess samples, so that shapes and dtypes are correct.
    if preprocess_feature is not None:
        sample_feature = preprocess_feature(sample_feature)
    if preprocess_label is not None:
        sample_label = preprocess_label(sample_feature, sample_label)

    config = {"num_threads": num_threads,
              "features": sample_feature.keys(),
              "labels": sample_label.keys()}
    for k in sample_feature.keys():
        config["feature_" + k] = {"shape": sample_feature[k].shape, "dtype": sample_feature[k].dtype.name}
    for k in sample_label.keys():
        config["label_" + k] = {"shape": sample_label[k].shape, "dtype": sample_label[k].dtype.name}

    with open(prefix + '_config.json', 'w') as outfile:
        json.dump(config, outfile)

    pool = Pool(processes=num_threads)
    pool.map(_write_tf_record_pool_helper, args)


def read_data(prefix, batch_size):
    folder = "/".join(prefix.split("/")[:-1])
    phase = prefix.split("/")[-1]
    config = json.load(open(prefix + '_config.json'))
    num_threads = config["num_threads"]

    filenames = [folder + "/" + f for f in listdir(folder) if isfile(join(folder, f)) and phase in f and not "config.json" in f]

    # Create a tf object for the filename list and the readers.
    filename_queue = tf.train.string_input_producer(filenames)
    readers = [_read_tf_record(filename_queue, config) for _ in range(num_threads)]

    batch_dict = tf.train.shuffle_batch_join(
        readers,
        batch_size=batch_size,
        capacity=10 * batch_size,
        min_after_dequeue=5 * batch_size
    )

    # Add batch dimension to feature and label shape

    feature_batch = {}
    label_batch = {}
    for k in batch_dict.keys():
        shape = tuple([batch_size] + list(config[k]["shape"]))
        tensor = tf.reshape(batch_dict[k], shape, name="input/"+phase+"/" + k + "_reshape")
        if "feature_" in k:
            feature_batch["_".join(k.split("_")[1:])] = tensor
        if "label_" in k:
            label_batch["_".join(k.split("_")[1:])] = tensor

    return feature_batch, label_batch


def auto_read_write_data(hyper_params, generate_data_fn, data_tmp_folder, force_generate_data=False, preprocess_feature=None, preprocess_label=None):
    if force_generate_data or not os.path.exists(data_tmp_folder):
        if not os.path.exists(data_tmp_folder):
            os.makedirs(data_tmp_folder)
        # Create training data.
        train_data, validation_data = generate_data_fn()

        # Write tf records
        write_data(os.path.join(data_tmp_folder, PHASE_TRAIN), train_data[0], train_data[1], 4, preprocess_feature, preprocess_label)
        write_data(os.path.join(data_tmp_folder, PHASE_VALIDATION), validation_data[0], validation_data[1], 2, preprocess_feature, preprocess_label)

    # Load data with tf records.
    train_features, train_labels = read_data(os.path.join(data_tmp_folder, PHASE_TRAIN), hyper_params.train.batch_size)
    validation_features, validation_labels = read_data(os.path.join(data_tmp_folder, PHASE_VALIDATION), hyper_params.train.validation_batch_size)

    return train_features, train_labels, validation_features, validation_labels