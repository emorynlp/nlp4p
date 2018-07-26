# ========================================================================
# Copyright 2017 Emory University
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
from types import SimpleNamespace

import numpy as np

from .component import SequenceTagger
from .model import conv2d_args, hidden_args
from .state import OPLRState
from .structure import TOK, POS
from .util import Accuracy, tsv_reader, json_reader
from .vsm import FastText, Word2Vec, get_loc_embeddings, get_vsm_embeddings, x_extract

__author__ = 'Jinho D. Choi'


class POSState(OPLRState):
    def __init__(self, document, vsm_list, label_map, feature_windows, padout):
        """
        POSState inherits the left-to-right one-pass (LR1P) decoding strategy from ForwardState.
        :param document: an input document.
        :type document: elit.structure.Document
        :param vsm_list: a list of vector space models.
        :type vsm_list: list of elit.vsm.VectorSpaceModel
        :param label_map: the mapping between class labels and their unique IDs.
        :type label_map: elit.vsm.LabelMap
        :param feature_windows: contextual feature_windows for feature extraction.
        :type feature_windows: tuple of int
        :param padout: a zero vector of size `num_class`; used to pad-pad label embeddings.
        :type padout: numpy.array
        """
        super().__init__(document, label_map, padout, POS)
        self.feature_windows = feature_windows

        # initialize embeddings
        self.embs = [get_vsm_embeddings(vsm, document, TOK) for vsm in vsm_list]
        self.embs.append(get_loc_embeddings(document))
        if padout: self.embs.append(self._get_label_embeddings())

    def eval(self, metric):
        """
        :param metric: the accuracy metric.
        :type metric: elit.util.Accuracy
        """
        autos = self.labels

        for i, sentence in enumerate(self.document):
            gold = sentence[POS]
            auto = autos[i]
            metric.correct += len([1 for g, p in zip(gold, auto) if g == p])
            metric.total += len(gold)

    @property
    def x(self):
        """
        :return: the n * d matrix where n = # of feature_windows and d = sum(vsm_list) + position emb + label emb
        """
        t = len(self.document.get_sentence(self.sen_id))
        l = ([x_extract(self.tok_id, w, t, emb[self.sen_id], pad) for w in self.feature_windows] for emb, pad in self.embs)
        n = np.column_stack(l)
        return n


class POSTagger(SequenceTagger):
    def __init__(self, ctx, vsm_list):
        """
        A part-of-speech tagger.
        :param ctx: "[cg]\d*"; the context (e.g., CPU or GPU) to process.
        :type ctx: str
        :param vsm_list: a list of vector space models (must include at least one).
        :type vsm_list: list of elit.vsm.VectorSpaceModel
        """
        super().__init__(ctx, vsm_list)

    # override
    def create_state(self, document):
        return POSState(document, self.vsm_list, self.label_map, self.feature_windows, self.padout)

    # override
    def eval_metric(self):
        return Accuracy()


# ======================================== Command Line ========================================

def train_args():
    def reader(s):
        """
        :param s: (tsv|json)(;\d:\d)*
        :return: reader, SimpleNamespace(tok, pos)
        """
        r = s.split(';')
        if r[0] == 'tsv':
            t = r[1].split(':')
            return tsv_reader, SimpleNamespace(tok=int(t[0]), pos=int(t[1]))
        else:
            return json_reader, None

    def int_tuple(s):
        """
        :param s: \d(,\d)*
        :return: tuple of int
        """
        return tuple(map(int, s.split(',')))

    parser = argparse.ArgumentParser('Train: part-of-speech tagging')

    # data
    parser.add_argument('-t', '--trn_path', type=str, metavar='filepath',
                        help='path to the training data (input)')
    parser.add_argument('-d', '--dev_path', type=str, metavar='filepath',
                        help='path to the development data (input)')
    parser.add_argument('-m', '--model_path', type=str, metavar='filepath', default=None,
                        help='path to the model data (output)')
    parser.add_argument('-r', '--reader', type=reader, metavar='(tsv|json)(;\d:\d)*',
                        default=(tsv_reader, SimpleNamespace(tok=0, pos=1)),
                        help='reader configuration')

    # lexicon
    parser.add_argument('-wv', '--word_vsm', type=str, metavar='filepath',
                        help='vector space model for word embeddings')
    parser.add_argument('-av', '--ambi_vsm', type=str, metavar='filepath', default=None,
                        help='vector space model for ambiguity classes')

    # configuration
    parser.add_argument('-le', '--label_embedding', type=bool, metavar='boolean', default=False,
                        help='if set, use label embeddings as features')
    parser.add_argument('-fw', '--feature_windows', type=int_tuple, metavar='int[,int]*',
                        default=tuple(range(-3, 4)),
                        help='contextual windows for feature extraction')
    parser.add_argument('-nc', '--num_class', type=int, metavar='int', default=50,
                        help='number of classes (part-of-speech tags)')
    parser.add_argument('-ir', '--input_dropout', type=float, metavar='float', default=0.0,
                        help='dropout rate applied to the input layer')
    parser.add_argument('-cc', '--conv2d_config', type=conv2d_args,
                        metavar='(ngram:filters:activation:pool:dropout)(;#1)*',
                        default=tuple(SimpleNamespace(ngram=i, filters=128, activation='relu', pool=None, dropout=0.2) for i in range(1, 5)),
                        help='configuration for the convolution layer')
    parser.add_argument('-hc', '--hidden_config', type=hidden_args, metavar='(dim:activation:dropout)(;#1)*', default=None,
                        help='configuration for the hidden layer')

    # training
    parser.add_argument('-cx', '--ctx', type=str, metavar='[cg]\d', default='c0',
                        help='device context')
    parser.add_argument('-ep', '--epoch', type=int, metavar='int', default=50,
                        help='number of epochs')
    parser.add_argument('-tb', '--trn_batch', type=int, metavar='int', default=64,
                        help='batch size for training')
    parser.add_argument('-db', '--dev_batch', type=int, metavar='int', default=4096,
                        help='batch size for evaluation')
    parser.add_argument('-op', '--optimizer', type=str, metavar='str', default='adagrad',
                        help='optimizer algorithm')
    parser.add_argument('-lr', '--learning_rate', type=float, metavar='float', default=0.01,
                        help='learning rate')
    parser.add_argument('-wd', '--weight_decay', type=float, metavar='float', default=0.0,
                        help='weight decay')

    args = parser.parse_args()
    return args


def train():
    # cml arguments
    args = train_args()

    # vector space models
    vsm_list = [FastText(args.word_vsm)]
    if args.ambi_vsm: vsm_list.append(Word2Vec(args.ambi_vsm))

    # component
    comp = POSTagger(args.ctx, vsm_list)
    comp.init(args.label_embedding, args.feature_windows, args.num_class, args.input_dropout, args.conv2d_config, args.hidden_config)

    # data
    reader, reader_args = args.reader
    trn_data = reader(args.trn_path, reader_args)
    dev_data = reader(args.dev_path, reader_args)

    # train
    comp.train(trn_data, dev_data, args.model_path, args.trn_batch, args.dev_batch, args.epoch, args.optimizer, args.learning_rate, args.weight_decay)


def evaluate():
    # cml arguments
    args = train_args()
    print(args.hidden_config)

    # # vector space models
    # vsm_list = [FastText(args.word_vsm)]
    # if args.ambi_vsm: vsm_list.append(Word2Vec(args.ambi_vsm))
    #
    # # component
    # comp = POSTagger(args.ctx, vsm_list)
    # comp.load(args.model_path)
    #
    # # data
    # reader, reader_args = args.reader
    # dev_data = reader(args.dev_path, reader_args)
    #
    # # decode
    # states = group_states(dev_data, comp.create_state)
    # e = comp._evaluate(states, reset=True)
    # print('DEV: %5.2f (%d/%d)' % (e.get(), e.correct, e.total))


if __name__ == '__main__':
    train()
    # evaluate()