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
"""Audio model specification."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import abc
import csv
import io
import os
import tempfile

import tensorflow as tf
from tensorflow_examples.lite.model_maker.core.api.api_util import mm_export
from tensorflow_examples.lite.model_maker.core.task import model_util
import tensorflow_hub as hub

try:
  from tflite_support.metadata_writers import audio_classifier as md_writer  # pylint: disable=g-import-not-at-top
  from tflite_support.metadata_writers import metadata_info as md_info  # pylint: disable=g-import-not-at-top
  from tflite_support.metadata_writers import writer_utils  # pylint: disable=g-import-not-at-top
  ENABLE_METADATA = True
except ImportError:
  ENABLE_METADATA = False


def _ensure_tf25(version):
  if version < '2.5':
    raise RuntimeError(
        'Audio Tasks requires TF2.5 or later. For example, you can run the '
        'following command to install TF2.5.0rc2:\n\n'
        'pip3 install tensorflow==2.5.0rc2\n\n')


def _get_tf_version():
  return tf.__version__


class BaseSpec(abc.ABC):
  """Base model spec for audio classification."""

  def __init__(self, model_dir=None, strategy=None):
    _ensure_tf25(_get_tf_version())
    self.model_dir = model_dir
    if not model_dir:
      self.model_dir = tempfile.mkdtemp()
    tf.compat.v1.logging.info('Checkpoints are stored in %s', self.model_dir)
    self.strategy = strategy or tf.distribute.get_strategy()

  @abc.abstractproperty
  def target_sample_rate(self):
    pass

  @abc.abstractmethod
  def create_model(self, num_classes, train_whole_model=False):
    pass

  @abc.abstractmethod
  def run_classifier(self, model, epochs, train_ds, validation_ds, **kwargs):
    pass

  def preprocess_ds(self, ds, is_training=False, cache_fn=None):
    """Returns a preprocessed dataset."""
    _ = is_training
    _ = cache_fn
    return ds

  def get_default_quantization_config(self):
    """Gets the default quantization configuration."""
    return None


def _remove_suffix_if_possible(text, suffix):
  return text.rsplit(suffix, 1)[0]


TFJS_MODEL_ROOT = 'https://storage.googleapis.com/tfjs-models/tfjs'


def _load_browser_fft_preprocess_model():
  """Load a model replicating WebAudio's AnalyzerNode.getFloatFrequencyData."""
  model_name = 'sc_preproc_model'
  file_extension = '.tar.gz'
  filename = model_name + file_extension
  # Load the preprocessing model, which transforms audio waveform into
  # spectrograms (2D image-like representation of sound).
  # This model replicates WebAudio's AnalyzerNode.getFloatFrequencyData
  # (https://developer.mozilla.org/en-US/docs/Web/API/AnalyserNode/getFloatFrequencyData).
  # It performs short-time Fourier transform (STFT) using a length-2048 Blackman
  # window. It opeartes on mono audio at the 44100-Hz sample rate.
  filepath = tf.keras.utils.get_file(
      filename,
      f'{TFJS_MODEL_ROOT}/speech-commands/conversion/{filename}',
      cache_subdir='model_maker',
      extract=True)
  model_path = _remove_suffix_if_possible(filepath, file_extension)
  return tf.keras.models.load_model(model_path)


def _load_tfjs_speech_command_model():
  """Download TFJS speech command model for fine-tune."""
  origin_root = f'{TFJS_MODEL_ROOT}/speech-commands/v0.3/browser_fft/18w'
  files_to_download = [
      'metadata.json', 'model.json', 'group1-shard1of2', 'group1-shard2of2'
  ]
  for filename in files_to_download:
    filepath = tf.keras.utils.get_file(
        filename,
        f'{origin_root}/{filename}',
        cache_subdir='model_maker/tfjs-sc-model')
  model_path = os.path.join(os.path.dirname(filepath), 'model.json')
  return model_util.load_tfjs_keras_model(model_path)


@mm_export('audio_classifier.BrowserFftSpec')
class BrowserFFTSpec(BaseSpec):
  """Model good at detecting speech commands, using Browser FFT spectrum."""

  def __init__(self, model_dir=None, strategy=None):
    """Initialize a new instance for BrowserFFT spec.

    Args:
      model_dir: The location to save the model checkpoint files.
      strategy: An instance of TF distribute strategy. If none, it will use the
        default strategy (either SingleDeviceStrategy or the current scoped
        strategy.
    """
    super(BrowserFFTSpec, self).__init__(model_dir, strategy)
    self._preprocess_model = _load_browser_fft_preprocess_model()
    self._tfjs_sc_model = _load_tfjs_speech_command_model()

    self.expected_waveform_len = self._preprocess_model.input_shape[-1]

  @property
  def target_sample_rate(self):
    return 44100

  @tf.function
  def _ensure_length(self, wav, unused_label):
    return len(wav) >= self.expected_waveform_len

  @tf.function
  def _split(self, wav, label):
    """Split the long audio samples into multiple trunks."""
    # wav shape: (audio_samples, )
    chunks = tf.math.floordiv(len(wav), self.expected_waveform_len)
    unused = tf.math.floormod(len(wav), self.expected_waveform_len)
    # Drop unused data
    wav = wav[:len(wav) - unused]
    # Split the audio sample into multiple chunks
    wav = tf.reshape(wav, (chunks, 1, self.expected_waveform_len))

    return wav, tf.repeat(tf.expand_dims(label, 0), len(wav))

  @tf.function
  def _preprocess(self, x, label):
    # x has shape (1, expected_waveform_len)
    spectrum = self._preprocess_model(x)
    # y has shape (1, embedding_len)
    spectrum = tf.squeeze(spectrum, axis=0)
    # y has shape (embedding_len,)
    return spectrum, label

  def preprocess_ds(self, ds, is_training=False, cache_fn=None):
    del is_training

    autotune = tf.data.AUTOTUNE
    ds = ds.filter(self._ensure_length)
    ds = ds.map(self._split, num_parallel_calls=autotune).unbatch()
    ds = ds.map(self._preprocess, num_parallel_calls=autotune)
    if cache_fn:
      ds = cache_fn(ds)
    return ds

  def create_model(self, num_classes, train_whole_model=False):
    if num_classes <= 1:
      raise ValueError(
          'AudioClassifier expects `num_classes` to be greater than 1')
    model = tf.keras.Sequential()
    for layer in self._tfjs_sc_model.layers[:-1]:
      model.add(layer)
    model.add(
        tf.keras.layers.Dense(
            name='classification_head', units=num_classes,
            activation='softmax'))
    if not train_whole_model:
      # Freeze all but the last layer of the model. The last layer will be
      # fine-tuned during transfer learning.
      for layer in model.layers[:-1]:
        layer.trainable = False
    return model

  def run_classifier(self, model, epochs, train_ds, validation_ds, **kwargs):
    model.compile(
        optimizer='adam', loss='categorical_crossentropy', metrics=['acc'])

    hist = model.fit(
        train_ds, validation_data=validation_ds, epochs=epochs, **kwargs)
    return hist

  def create_serving_model(self, training_model):
    """Create a model for serving."""
    combined = tf.keras.Sequential()
    combined.add(self._preprocess_model)
    combined.add(training_model)
    # Build the model.
    combined.build([None, self.expected_waveform_len])
    return combined

  def export_tflite(self,
                    model,
                    tflite_filepath,
                    with_metadata=True,
                    export_metadata_json_file=True,
                    index_to_label=None):
    """Converts the retrained model to tflite format and saves it.

    This method overrides the default `CustomModel._export_tflite` method, and
    include the pre-processing in the exported TFLite library since support
    library can't handle audio tasks yet.

    Args:
      model: An instance of the keras classification model to be exported.
      tflite_filepath: File path to save tflite model.
      with_metadata: Whether the output tflite model contains metadata.
      export_metadata_json_file: Whether to export metadata in json file. If
        True, export the metadata in the same directory as tflite model.Used
        only if `with_metadata` is True.
      index_to_label: A list that map from index to label class name.
    """
    del with_metadata, export_metadata_json_file, index_to_label
    combined = self.create_serving_model(model)

    # Sets batch size from None to 1 when converting to tflite.
    model_util.set_batch_size(model, batch_size=1)

    model_util.export_tflite(
        combined,
        tflite_filepath,
        quantization_config=None,
        supported_ops=(tf.lite.OpsSet.TFLITE_BUILTINS,
                       tf.lite.OpsSet.SELECT_TF_OPS))

    # Sets batch size back to None to support retraining later.
    model_util.set_batch_size(model, batch_size=None)


@mm_export('audio_classifier.YamNetSpec')
class YAMNetSpec(BaseSpec):
  """Model good at detecting environmental sounds, using YAMNet embedding."""

  EXPECTED_WAVEFORM_LENGTH = 15600  # effectively 0.975s
  EMBEDDING_SIZE = 1024

  # Information used to populate TFLite metadata.
  _MODEL_NAME = 'yamnet/classification'
  _MODEL_DESCRIPTION = 'Recognizes sound events'
  _MODEL_VERSION = 'v1'
  _MODEL_AUTHOR = 'TensorFlow Lite Model Maker'
  _MODEL_LICENSES = ('Apache License. Version 2.0 '
                     'http://www.apache.org/licenses/LICENSE-2.0.')

  _SAMPLE_RATE = 16000
  _CHANNELS = 1

  _INPUT_NAME = 'audio_clip'
  _INPUT_DESCRIPTION = 'Input audio clip to be classified.'

  _YAMNET_OUTPUT_NAME = 'scores'
  _YAMNET_OUTPUT_DESCRIPTION = ('Scores in range 0..1.0 for each of the 521 '
                                'output classes.')
  _YAMNET_LABEL_FILE = 'yamnet_label_list.txt'

  _CUSTOM_OUTPUT_NAME = 'classification'
  _CUSTOM_OUTPUT_DESCRIPTION = (
      'Scores in range 0..1.0 for each output classes.')
  _CUSTOM_LABEL_FILE = 'custom_label_list.txt'

  def __init__(
      self,
      model_dir: None = None,
      strategy: None = None,
      yamnet_model_handle='https://tfhub.dev/google/yamnet/1',
      frame_length=EXPECTED_WAVEFORM_LENGTH,  # Window size 0.975 s
      frame_step=EXPECTED_WAVEFORM_LENGTH // 2,  # Hop of 0.975 /2 s
      keep_yamnet_and_custom_heads=True):
    """Initialize a new instance for YAMNet spec.

    Args:
      model_dir: The location to save the model checkpoint files.
      strategy: An instance of TF distribute strategy. If none, it will use the
        default strategy (either SingleDeviceStrategy or the current scoped
        strategy.
      yamnet_model_handle: Path of the TFHub model for retrining.
      frame_length: The number of samples in each audio frame. If the audio file
        is shorter than `frame_length`, then the audio file will be ignored.
      frame_step: The number of samples between two audio frames. This value
        should be bigger than `frame_length`.
      keep_yamnet_and_custom_heads: Boolean, decides if the final TFLite model
        contains both YAMNet and custom trained classification heads. When set
        to False, only the trained custom head will be preserved.
    """
    super(YAMNetSpec, self).__init__(model_dir, strategy)
    self._yamnet_model_handle = yamnet_model_handle
    self._yamnet_model = hub.load(yamnet_model_handle)
    self._frame_length = frame_length
    self._frame_step = frame_step
    self._keep_yamnet_and_custom_heads = keep_yamnet_and_custom_heads

  @property
  def target_sample_rate(self):
    return self._SAMPLE_RATE

  def create_model(self, num_classes, train_whole_model=False):
    model = tf.keras.Sequential([
        tf.keras.layers.InputLayer(
            input_shape=(YAMNetSpec.EMBEDDING_SIZE),
            dtype=tf.float32,
            name='embedding'),
        tf.keras.layers.Dense(
            num_classes, name='classification_head', activation='softmax')
    ])
    return model

  def run_classifier(self, model, epochs, train_ds, validation_ds, **kwargs):
    model.compile(
        optimizer='adam', loss='categorical_crossentropy', metrics=['acc'])

    hist = model.fit(
        train_ds, validation_data=validation_ds, epochs=epochs, **kwargs)
    return hist

  # Annotate the TF function with input_signature to avoid re-tracing. Otherwise
  # the TF function gets retraced everytime the input shape is changed.
  # Check https://www.tensorflow.org/api_docs/python/tf/function#args_1 for more
  # information.
  @tf.function(input_signature=[
      tf.TensorSpec(shape=[None], dtype=tf.float32),
      tf.TensorSpec([], dtype=tf.int32)
  ])
  def _frame(self, wav, label):
    clips = tf.signal.frame(
        wav, frame_length=self._frame_length, frame_step=self._frame_step)
    batch_labels = tf.repeat(tf.expand_dims(label, 0), len(clips))

    return clips, batch_labels

  @tf.function(input_signature=[
      tf.TensorSpec(shape=[None], dtype=tf.float32),
      tf.TensorSpec([], dtype=tf.int32)
  ])
  def _extract_embedding(self, wav, label):
    _, embeddings, _ = self._yamnet_model(wav)  # (chunks, EMBEDDING_SIZE)
    embedding = tf.reduce_mean(embeddings, axis=0)
    return embedding, label

  @tf.function(input_signature=[
      tf.TensorSpec(shape=[EMBEDDING_SIZE], dtype=tf.float32),
      tf.TensorSpec([], dtype=tf.int32)
  ])
  def _add_noise(self, embedding, label):
    noise = tf.random.normal(
        embedding.shape, mean=0.0, stddev=.2, dtype=tf.dtypes.float32)
    return noise + embedding, label

  def preprocess_ds(self, ds, is_training=False, cache_fn=None):
    autotune = tf.data.AUTOTUNE
    ds = ds.map(self._frame, num_parallel_calls=autotune).unbatch()
    ds = ds.map(self._extract_embedding, num_parallel_calls=autotune)

    # Cache intermediate results right before data augmentation.
    if cache_fn:
      ds = cache_fn(ds)

    if is_training:
      ds = ds.map(self._add_noise, num_parallel_calls=autotune)
    return ds

  def _yamnet_labels(self):
    class_map_path = self._yamnet_model.class_map_path().numpy()
    class_map_csv_text = tf.io.read_file(class_map_path).numpy().decode('utf-8')
    class_map_csv = io.StringIO(class_map_csv_text)
    class_names = [
        display_name for (class_index, mid,
                          display_name) in csv.reader(class_map_csv)
    ]
    class_names = class_names[1:]  # Skip CSV header
    return class_names

  def _export_labels(self, filepath, index_to_label):
    with tf.io.gfile.GFile(filepath, 'w') as f:
      f.write('\n'.join(index_to_label))

  def _export_metadata(self, tflite_filepath, index_to_label,
                       export_metadata_json_file):
    """Export TFLite metadata."""
    with tempfile.TemporaryDirectory() as temp_dir:
      # Prepare metadata
      with open(tflite_filepath, 'rb') as f:
        model_buffer = f.read()

      general_md = md_info.GeneralMd(
          name=self._MODEL_NAME,
          description=self._MODEL_DESCRIPTION,
          version=self._MODEL_VERSION,
          author=self._MODEL_AUTHOR,
          licenses=self._MODEL_LICENSES)
      input_md = md_info.InputAudioTensorMd(self._INPUT_NAME,
                                            self._INPUT_DESCRIPTION,
                                            self._SAMPLE_RATE, self._CHANNELS)

      # Save label files.
      custom_label_filepath = os.path.join(temp_dir, self._CUSTOM_LABEL_FILE)
      self._export_labels(custom_label_filepath, index_to_label)

      custom_output_md = md_info.ClassificationTensorMd(
          name=self._CUSTOM_OUTPUT_NAME,
          description=self._CUSTOM_OUTPUT_DESCRIPTION,
          label_files=[
              md_info.LabelFileMd(
                  file_path=os.path.join(temp_dir, self._CUSTOM_LABEL_FILE))
          ],
          tensor_type=writer_utils.get_output_tensor_types(model_buffer)[-1],
          score_calibration_md=None)

      if self._keep_yamnet_and_custom_heads:
        yamnet_label_filepath = os.path.join(temp_dir, self._YAMNET_LABEL_FILE)
        self._export_labels(yamnet_label_filepath, self._yamnet_labels())

        yamnet_output_md = md_info.ClassificationTensorMd(
            name=self._YAMNET_OUTPUT_NAME,
            description=self._YAMNET_OUTPUT_DESCRIPTION,
            label_files=[
                md_info.LabelFileMd(
                    file_path=os.path.join(temp_dir, self._YAMNET_LABEL_FILE))
            ],
            tensor_type=writer_utils.get_output_tensor_types(model_buffer)[0],
            score_calibration_md=None)
        output_md = [yamnet_output_md, custom_output_md]
      else:
        output_md = [custom_output_md]

      # Populate metadata
      writer = md_writer.MetadataWriter.create_from_metadata_info_for_multihead(
          model_buffer=model_buffer,
          general_md=general_md,
          input_md=input_md,
          output_md_list=output_md)

      output_model = writer.populate()

      with tf.io.gfile.GFile(tflite_filepath, 'wb') as f:
        f.write(output_model)

      if export_metadata_json_file:
        metadata_json = writer.get_metadata_json()
        export_json_file = os.path.splitext(tflite_filepath)[0] + '.json'
        with open(export_json_file, 'w') as f:
          f.write(metadata_json)

  def create_serving_model(self, training_model):
    """Create a model for serving."""
    embedding_extraction_layer = hub.KerasLayer(
        self._yamnet_model_handle, trainable=False)
    keras_input = tf.keras.Input(
        shape=(YAMNetSpec.EXPECTED_WAVEFORM_LENGTH,),
        dtype=tf.float32,
        name='audio')  # (1, wav)
    reshaped_input = tf.reshape(keras_input,
                                (YAMNetSpec.EXPECTED_WAVEFORM_LENGTH,))  # (wav)

    scores, embeddings, _ = embedding_extraction_layer(reshaped_input)
    serving_outputs = training_model(embeddings)

    if self._keep_yamnet_and_custom_heads:
      serving_model = tf.keras.Model(keras_input, [scores, serving_outputs])
    else:
      serving_model = tf.keras.Model(keras_input, serving_outputs)

    return serving_model

  def export_tflite(self,
                    model,
                    tflite_filepath,
                    with_metadata=True,
                    export_metadata_json_file=True,
                    index_to_label=None):
    """Converts the retrained model to tflite format and saves it.

    This method overrides the default `CustomModel._export_tflite` method, and
    include the spectrom extraction in the model.

    The exported model has input shape (1, number of wav samples)

    Args:
      model: An instance of the keras classification model to be exported.
      tflite_filepath: File path to save tflite model.
      with_metadata: Whether the output tflite model contains metadata.
      export_metadata_json_file: Whether to export metadata in json file. If
        True, export the metadata in the same directory as tflite model. Used
        only if `with_metadata` is True.
      index_to_label: A list that map from index to label class name.
    """
    serving_model = self.create_serving_model(model)

    # TODO(b/164229433): Remove SELECT_TF_OPS once changes in the bug are
    # released.
    model_util.export_tflite(
        serving_model,
        tflite_filepath,
        quantization_config=None,
        supported_ops=(tf.lite.OpsSet.TFLITE_BUILTINS,
                       tf.lite.OpsSet.SELECT_TF_OPS))

    if with_metadata:
      if not ENABLE_METADATA:
        print('Writing Metadata is not support in the current tflite-support '
              'version. Please use tflite-support >= 0.2.*')
      else:
        self._export_metadata(tflite_filepath, index_to_label,
                              export_metadata_json_file)
