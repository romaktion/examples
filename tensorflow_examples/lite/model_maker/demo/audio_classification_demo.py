# Copyright 2020 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Audio classification demo code of Model Maker for TFLite.

Example usage:
python audio_classification_demo.py --export_dir=/tmp

Sample output:
Downloading data from
https://storage.googleapis.com/download.tensorflow.org/data/mini_speech_commands.zip
182083584/182082353 [==============================] - 4s 0us/step
182091776/182082353 [==============================] - 4s 0us/step
Dataset has been downloaded to
/usr/local/google/home/wangtz/.keras/datasets/mini_speech_commands
Processing audio files:
8000/8000 [==============================] - 354s 44ms/file
Cached 7178 audio samples.
Training the model
5742/5742 [==============================] - 29s 5ms/step - loss: 3.2289 - acc:
0.8029 - val_loss: 0.6229 - val_acc: 0.9638
Evaluating the model
15/15 [==============================] - 2s 12ms/step - loss: 1.3569 - acc:
0.9270
Test accuracy: 0.927039
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
from absl import app
from absl import flags
from absl import logging

import tensorflow as tf
from tensorflow_examples.lite.model_maker.core.data_util import audio_dataloader
from tensorflow_examples.lite.model_maker.core.task import audio_classifier
from tensorflow_examples.lite.model_maker.core.task import model_spec

FLAGS = flags.FLAGS


def define_flags():
  flags.DEFINE_string('export_dir', None,
                      'The directory to save exported files.')
  flags.DEFINE_string('spec', 'audio_browser_fft',
                      'Name of the model spec to use.')
  flags.DEFINE_string(
      'dataset', 'mini_speech_command',
      'Which dataset to use. Supports: `mini_speech_command` and `esc50`')
  flags.mark_flag_as_required('export_dir')


def download_speech_commands_dataset(**kwargs):
  """Downloads demo dataset, and returns directory path."""
  tf.compat.v1.logging.info('Downloading mini speech command dataset.')
  # ${HOME}/.keras/datasets/mini_speech_commands.zip
  filepath = tf.keras.utils.get_file(
      fname='mini_speech_commands.zip',
      origin='https://storage.googleapis.com/download.tensorflow.org/data/mini_speech_commands.zip',
      extract=True,
      **kwargs)
  # ${HOME}/.keras/datasets/mini_speech_commands
  folder_path = filepath.rsplit('.', 1)[0]
  print(f'Dataset has been downloaded to {folder_path}')
  return folder_path


def download_esc50_dataset(**kwargs):
  """Downloads ESC50 dataset, and returns directory path."""
  tf.compat.v1.logging.info('Downloading ESC50 dataset.')
  # ${HOME}/.keras/datasets/mini_speech_commands.zip
  filepath = tf.keras.utils.get_file(
      'esc-50.zip',
      'https://github.com/karoldvl/ESC-50/archive/master.zip',
      cache_subdir='datasets',
      extract=True,
      **kwargs)
  # ${HOME}/.keras/datasets/mini_speech_commands
  folder_path = filepath.rsplit('/', 1)[0]
  folder_path = os.path.join(folder_path, 'ESC-50-master')

  print(f'Dataset has been downloaded to {folder_path}')
  return folder_path


def run(spec, data_dir, dataset_type, export_dir, **kwargs):
  """Runs demo."""
  spec = model_spec.get(spec)

  if dataset_type == 'esc50':
    # Limit to 2 categories to speed up the demo
    categories = ['dog', 'cat']
    train_data = audio_dataloader.DataLoader.from_esc50(
        spec, data_dir, folds=[0, 1, 2, 3], categories=categories)
    validation_data = audio_dataloader.DataLoader.from_esc50(
        spec, data_dir, folds=[
            4,
        ], categories=categories)
    test_data = audio_dataloader.DataLoader.from_esc50(
        spec, data_dir, folds=[
            5,
        ], categories=categories)

  else:
    data = audio_dataloader.DataLoader.from_folder(spec, data_dir)
    train_data, rest_data = data.split(0.8)
    validation_data, test_data = rest_data.split(0.5)

  print('Training the model')
  model = audio_classifier.create(train_data, spec, validation_data, **kwargs)

  print('Evaluating the model')
  _, acc = model.evaluate(test_data)
  print('Test accuracy: %f' % acc)

  model.export(export_dir)


def main(_):
  logging.set_verbosity(logging.INFO)

  if FLAGS.dataset == 'esc50':
    data_dir = download_esc50_dataset()
  else:
    data_dir = download_speech_commands_dataset()

  run(FLAGS.spec, data_dir, FLAGS.dataset, export_dir=FLAGS.export_dir)


if __name__ == '__main__':
  define_flags()
  app.run(main)
