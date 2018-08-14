# ========================================================================
# Copyright 2018 Emory University
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
# ========================================================================
import argparse
import logging
import pickle
import sys
import time
from types import SimpleNamespace
from typing import Tuple, Optional, Union, Sequence

import mxnet as mx
import numpy as np

from elit.cli import ComponentCLI
from elit.component import MXNetComponent
from elit.eval import Accuracy, F1
from elit.iterator import SequenceIterator, BatchIterator
from elit.model import FFNNModel, namespace_input, namespace_output
from elit.state import NLPState
from elit.structure import Document, TOK
from elit.util.cli import args_reader, args_vsm, args_tuple_int, args_conv2d, args_hidden, args_loss, args_context, args_dict_str_float, set_logger
from elit.util.io import pkl, gln, group_states
from elit.util.util import BILOU, to_gold, to_out
from elit.vsm import LabelMap, x_extract, Position2Vec

__author__ = 'Jinho D. Choi'


# ======================================== State ========================================

class TokenTaggerState(NLPState):
    """
    :class:`TokenTaggingState` generates a state per token using the one-pass left-to-right decoding strategy
    and predicts tags for individual tokens.
    """

    def __init__(self,
                 document: Document,
                 key: str,
                 label_map: LabelMap,
                 vsm_list: Sequence[SimpleNamespace],
                 feature_windows: Tuple[int, ...]):
        """
        :param document: the input document.
        :param key: the key to each sentence in the document where predicted tags are to be saved.
        :param label_map: collects labels during training and maps them to unique class IDs.
        :param vsm_list: the sequence of namespace(:class:`elit.vsm.VectorSpaceModel`, key).
        :param feature_windows: the contextual windows for feature extraction.
        """
        super().__init__(document, key)
        self.label_map = label_map
        self.feature_windows = feature_windows

        # initialize gold-standard labels if available
        key_gold = to_gold(key)
        self.gold = [s[key_gold] for s in document] if key_gold in document.sentences[0] else None

        # initialize output and predicted tags
        self.pred = []
        self.output = []

        # initialize embeddings
        self.embs = [(vsm.model.sentence_embedding_list(document, vsm.key), vsm.model.pad) for vsm in vsm_list]

        # the followings are initialized in self.init()
        self.sen_id = 0
        self.tok_id = 0
        self.init()

    def init(self):
        self.sen_id = 0
        self.tok_id = 0
        del self.pred[:]
        del self.output[:]

        key_out = to_out(self.key)
        for s in self.document:
            self.pred.append([None] * len(s))
            s[self.key] = self.pred[-1]
            self.output.append([None] * len(s))
            s[key_out] = self.output[-1]

    def process(self, output: Optional[np.ndarray] = None):
        """
        :param output: the predicted output of the current token.

        Assigns the predicted output to the current token, then processes to the next token.
        """
        # apply the output to the current token
        if output is not None:
            self.pred[self.sen_id][self.tok_id] = self.label_map.argmax(output)
            self.output[self.sen_id][self.tok_id] = output

        # process to the next token
        self.tok_id += 1
        if self.tok_id == len(self.document.sentences[self.sen_id]):
            self.sen_id += 1
            self.tok_id = 0

    def has_next(self) -> bool:
        """
        :return: ``False`` if no more token is left to be tagged; otherwise, ``True``.
        """
        return 0 <= self.sen_id < len(self.document)

    @property
    def x(self) -> np.ndarray:
        """
        :return: the feature matrix of the current token.
        """
        l = ([x_extract(self.tok_id, w, emb[self.sen_id], pad) for w in self.feature_windows] for emb, pad in self.embs)
        return np.column_stack(l)

    @property
    def y(self) -> Optional[int]:
        """
        :return: the class ID of the current token's gold-standard label if available; otherwise, None.
        """
        return self.label_map.add(self.gold[self.sen_id][self.tok_id]) if self.gold is not None else None


# ======================================== EvalMetric ========================================

class TokenAccuracy(Accuracy):
    def __init__(self, key: str):
        super().__init__()
        self.key = key

    def update(self, document: Document):
        for sentence in document:
            gold = sentence[to_gold(self.key)]
            pred = sentence[self.key]
            self.correct += len([1 for g, p in zip(gold, pred) if g == p])
            self.total += len(sentence)


class ChunkF1(F1):
    def __init__(self, key: str):
        super().__init__()
        self.key = key

    def update(self, document: Document):
        for sentence in document:
            gold = BILOU.to_chunks(sentence[to_gold(self.key)])
            pred = BILOU.to_chunks(sentence[self.key])
            self.correct += len(set.intersection(set(gold), set(pred)))
            self.p_total += len(pred)
            self.r_total += len(gold)


# ======================================== Component ========================================

class TokenTagger(MXNetComponent):
    """
    :class:`TokenTagger` provides an abstract class to implement a tagger that predicts a tag for every token.
    """

    def __init__(self,
                 ctx: mx.Context,
                 vsm_list: Sequence[SimpleNamespace]):
        """
        :param ctx: the (list of) device context(s) for :class:`mxnet.gluon.Block`.
        :param vsm_list: the sequence of namespace(model, key),
                         where the key indicates the key of the values to retrieve embeddings for (e.g., tok).
        """
        super().__init__(ctx)
        self.vsm_list = vsm_list

        # to be loaded/saved
        self.key = None
        self.sequence = None
        self.chunking = None
        self.label_map = None
        self.feature_windows = None
        self.position_embedding = None
        self.input_config = None
        self.output_config = None
        self.conv2d_config = None
        self.hidden_config = None

    def __str__(self):
        s = ('Token Tagger',
             '- type: %s' % ('chunking' if self.chunking else 'tagging'),
             '- sequence: %r' % self.sequence,
             '- feature windows: %s' % str(self.feature_windows),
             '- position embedding: %r' % self.position_embedding,
             '- %s' % str(self.model))
        return '\n'.join(s)

    # override
    def init(self,
             key: str,
             sequence: bool,
             chunking: bool,
             num_class: int,
             feature_windows: Tuple[int, ...],
             position_embedding: bool,
             input_dropout: float = 0.0,
             conv2d_config: Optional[Tuple[SimpleNamespace]] = None,
             hidden_config: Optional[Tuple[SimpleNamespace]] = None,
             initializer: mx.init.Initializer = None,
             **kwargs):
        """
        :param key: the key to each sentence where the tags are to be saved.
        :param sequence: if ``True``, run in sequence mode; otherwise, batch mode.
        :param chunking: if ``True``, run chunking instead of tagging.
        :param num_class: the number of classes (part-of-speech tags).
        :param feature_windows: the content windows for feature extraction.
        :param position_embedding: if ``True``, use position embeddings as features.
        :param input_dropout: the dropout rate to be applied to the input layer.
        :param conv2d_config: the configuration for n-gram 2D convolutions.
        :param hidden_config: the configuration for hidden layers
        :param initializer: the weight initializer for :class:`mxnet.gluon.Block`.
        :param kwargs: extra parameters for :class:`mxnet.gluon.Block`.
        """
        # configuration
        if initializer is None: initializer = mx.init.Xavier(magnitude=2.24, rnd_type='gaussian')
        input_dim = sum([vsm.model.dim for vsm in self.vsm_list])
        input_config = namespace_input(input_dim, row=len(feature_windows), dropout=input_dropout)
        output_config = namespace_output(num_class)

        # initialization
        self.key = key
        self.sequence = sequence
        self.chunking = chunking
        self.label_map = LabelMap()
        self.feature_windows = feature_windows
        self.position_embedding = position_embedding
        self.input_config = input_config
        self.output_config = output_config
        self.conv2d_config = conv2d_config
        self.hidden_config = hidden_config

        self.model = FFNNModel(self.input_config, self.output_config, self.conv2d_config, self.hidden_config, **kwargs)
        self.model.collect_params().initialize(initializer, ctx=self.ctx)
        logging.info(self.__str__())

    # override
    def load(self, model_path: str, **kwargs):
        """
        :param model_path: the path to a pre-trained model to be loaded.
        :type model_path: str
        """
        with open(pkl(model_path), 'rb') as fin:
            self.key = pickle.load(fin)
            self.sequence = pickle.load(fin)
            self.chunking = pickle.load(fin)
            self.label_map = pickle.load(fin)
            self.feature_windows = pickle.load(fin)
            self.position_embedding = pickle.load(fin)
            self.input_config = pickle.load(fin)
            self.output_config = pickle.load(fin)
            self.conv2d_config = pickle.load(fin)
            self.hidden_config = pickle.load(fin)

        self.model = FFNNModel(self.input_config, self.output_config, self.conv2d_config, self.hidden_config, **kwargs)
        self.model.collect_params().load(gln(model_path), self.ctx)
        logging.info(self.__str__())

    # override
    def save(self, model_path, **kwargs):
        """
        :param model_path: the filepath where the model is to be saved.
        :type model_path: str
        """
        with open(pkl(model_path), 'wb') as fout:
            pickle.dump(self.key, fout)
            pickle.dump(self.sequence, fout)
            pickle.dump(self.chunking, fout)
            pickle.dump(self.label_map, fout)
            pickle.dump(self.feature_windows, fout)
            pickle.dump(self.position_embedding, fout)
            pickle.dump(self.input_config, fout)
            pickle.dump(self.output_config, fout)
            pickle.dump(self.conv2d_config, fout)
            pickle.dump(self.hidden_config, fout)

        self.model.collect_params().save(gln(model_path))

    # override
    def data_iterator(self, documents: Sequence[Document], batch_size: int, shuffle: bool, label: bool, **kwargs) -> Union[BatchIterator, SequenceIterator]:
        if self.sequence:
            def create(d: Document) -> TokenTaggerState:
                return TokenTaggerState(d, self.key, self.label_map, self.vsm_list, self.feature_windows)

            states = group_states(documents, create)
        else:
            states = [TokenTaggerState(d, self.key, self.label_map, self.vsm_list, self.feature_windows) for d in documents]

        iterator = SequenceIterator if self.sequence else BatchIterator
        return iterator(states, batch_size, shuffle, label, **kwargs)

    # override
    def eval_metric(self) -> Union[TokenAccuracy, ChunkF1]:
        """
        :return: :class:`ChunkF1` if self.chunking else :class:`TokenAccuracy`.
        """
        return ChunkF1(self.key) if self.chunking else TokenAccuracy(self.key)


# ======================================== Command-Line Interface ========================================

class TokenTaggerCLI(ComponentCLI):
    def __init__(self):
        super().__init__('token_tagger', 'Token Tagger')

    # override
    @classmethod
    def train(cls, args):
        # create a arg-parser
        parser = argparse.ArgumentParser(description='Train a token tagger',
                                         formatter_class=argparse.ArgumentDefaultsHelpFormatter)

        # data
        group = parser.add_argument_group("data arguments")

        group.add_argument('-t', '--trn_path', type=str, metavar='filepath',
                           help='filepath to the training data (input)')
        group.add_argument('-d', '--dev_path', type=str, metavar='filepath',
                           help='filepath to the development data (input)')
        group.add_argument('-m', '--model_path', type=str, metavar='filepath',
                           default=None,
                           help='filepath to the model data (output); if not set, the model is not saved')
        group.add_argument('-r', '--reader', type=args_reader, metavar='json or tsv=(str:int)(,#1)*',
                           default=args_reader('json'),
                           help='type of the reader and its configuration to match the data format (default: json)')
        group.add_argument('-v', '--vsm_list', type=args_vsm, metavar='(fasttext|word2vec:key:filepath)( #1)*', nargs='+',
                           help='list of (type of vector space model, key, filepath)')
        group.add_argument('-l', '--log', type=str, metavar='filepath',
                           default=None,
                           help='filepath to the logging file; if not set, use stdout')

        # tagger
        group = parser.add_argument_group("tagger arguments")

        group.add_argument('-key', '--key', type=str, metavar='str',
                           help='key to the document dictionary where the predicted tags are to be stored')
        group.add_argument('-seq', '--sequence', action='store_true',
                           help='if set, run in sequence mode; otherwise, batch mode')
        group.add_argument('-chu', '--chunking', action='store_true',
                           help='if set, tag chunks (e.g., named entities); otherwise, tag tokens (e.g., part-of-speech tags)')
        group.add_argument('-fw', '--feature_windows', type=args_tuple_int, metavar='int(,int)*',
                           default=args_tuple_int('3,2,1,0,-1,-2,-3'),
                           help='content windows for feature extraction (default: 3,2,1,0,-1,-2,-3)')
        group.add_argument('-pe', '--position_embedding', action='store_true',
                           help='if set, use position embeddings as features')

        # network
        group = parser.add_argument_group("network arguments")

        group.add_argument('-nc', '--num_class', type=int, metavar='int',
                           help='number of classes')
        group.add_argument('-ir', '--input_dropout', type=float, metavar='float',
                           default=0.0,
                           help='dropout rate applied to the input layer (default: 0.0)')
        group.add_argument('-cc', '--conv2d_config', type=args_conv2d, metavar='(ngram:filters:activation:pool:dropout)( #1)*', nargs='+',
                           default=tuple(args_conv2d('%d:128:relu:none:0.2' % i) for i in range(1, 6)),
                           help='configuration for the convolution layer (default: %d:128:relu:none:0.2, where %d = [1..5])')
        group.add_argument('-hc', '--hidden_config', type=args_hidden, metavar='(dim:activation:dropout)( #1)*', nargs='+',
                           default=None,
                           help='configuration for the hidden layer (default: None)')

        # training
        group = parser.add_argument_group("training arguments")

        group.add_argument('-cx', '--context', type=args_context, metavar='[cg]int', nargs='+',
                           default=mx.cpu(),
                           help='device context(s)')
        group.add_argument('-ep', '--epoch', type=int, metavar='int', default=100,
                           help='number of epochs')
        group.add_argument('-tb', '--trn_batch', type=int, metavar='int', default=64,
                           help='batch size for the training data')
        group.add_argument('-db', '--dev_batch', type=int, metavar='int', default=2048,
                           help='batch size for the development data')
        group.add_argument('-lo', '--loss', type=args_loss, metavar='str', default=None,
                           help='loss function')
        group.add_argument('-op', '--optimizer', type=str, metavar='str', default='adagrad',
                           help='optimizer')
        group.add_argument('-opp', '--optimizer_params', type=args_dict_str_float, metavar='([A-Za-z0-9_-]+):(\\d+)',
                           default=args_dict_str_float('learning_rate:0.01'),
                           help='optimizer parameters')

        # arguments
        args = parser.parse_args(args)
        set_logger(args.log)

        args.vsm_list = [SimpleNamespace(model=n.type(n.filepath), key=n.key) for n in args.vsm_list]
        if isinstance(args.context, list) and len(args.context) == 1: args.context = args.context[0]
        if isinstance(args.conv2d_config, list) and args.conv2d_config[0] is None: args.conv2d_config = None
        if args.loss is None: args.loss = mx.gluon.loss.SoftmaxCrossEntropyLoss()
        initializer = mx.init.Xavier(magnitude=2.24, rnd_type='gaussian')

        # component
        comp = TokenTagger(args.context, args.vsm_list)
        comp.init(args.key, args.sequence, args.chunking, args.num_class, args.feature_windows, args.position_embedding, args.input_dropout, args.conv2d_config, args.hidden_config, initializer)

        # data
        trn_docs = args.reader.type(args.trn_path, args.reader.params, args.key)
        dev_docs = args.reader.type(args.dev_path, args.reader.params, args.key)

        # # train
        comp.train(trn_docs, dev_docs, args.model_path, args.trn_batch, args.dev_batch, args.epoch, args.loss, args.optimizer, args.optimizer_params)
        logging.info('# of classes: %d' % len(comp.label_map))

    # override
    @classmethod
    def decode(cls, args):
        pass

    # override
    @classmethod
    def evaluate(cls, args):
        # create a arg-parser
        parser = argparse.ArgumentParser(description='Evaluate the token tagger',
                                         formatter_class=argparse.ArgumentDefaultsHelpFormatter)

        # data
        group = parser.add_argument_group("data arguments")

        group.add_argument('-d', '--dev_path', type=str, metavar='filepath',
                           help='filepath to the development data (input)')
        group.add_argument('-m', '--model_path', type=str, metavar='filepath',
                           default=None,
                           help='filepath to the model data (output)')
        group.add_argument('-r', '--reader', type=args_reader, metavar='json or tsv=(str:int)(,#1)*',
                           default=args_reader('json'),
                           help='type of the reader and its configuration to match the data format (default: json)')

        # lexicon
        group = parser.add_argument_group("lexicon arguments")

        group.add_argument('-v', '--vsm_list', type=args_vsm, metavar='(fasttext|word2vec:key:filepath)( #1)*', nargs='+',
                           help='list of (type of vector space model, key, filepath)')

        # evaluation
        group = parser.add_argument_group("training arguments")

        group.add_argument('-cx', '--context', type=args_context, metavar='[cg]int', nargs='+',
                           default=mx.cpu(),
                           help='device context(s)')
        group.add_argument('-db', '--dev_batch', type=int, metavar='int', default=2048,
                           help='batch size for the development data')

        # arguments
        args = parser.parse_args(sys.argv[3:])
        args.vsm_list = tuple(SimpleNamespace(model=n.type(n.filepath), key=n.key) for n in args.vsm_list)
        if isinstance(args.context, list) and len(args.context) == 1: args.context = args.context[0]

        # component
        comp = TokenTagger(args.context, args.vsm_list)
        comp.load(args.model_path)

        # data
        dev_docs = args.reader.type(args.dev_path, args.reader.params, comp.key)

        # evaluate
        st = time.time()
        e = comp.evaluate(dev_docs, args.dev_batch)
        et = time.time()
        logging.info(str(e))
        logging.info('Time: %d' % (et - st))


if __name__ == '__main__':
    cli = TokenTaggerCLI()
