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
import logging
import random
from types import SimpleNamespace

import pickle
import os
import numpy as np
import mxnet as mx
import time
from mxnet import gluon, nd

from elit.nlp.component import CNN2DModel, NLPComponent, pkl, ForwardState, gln, CNNcharModel
from elit.nlp.lexicon import LabelMap, FastText, Word2Vec, NamedEntityTree, Pos2Vec, Cluster2Vec, Char2Vec
from elit.nlp.metric import F1
from elit.nlp.structure import TOKEN, NER, POS
from elit.nlp.util import x_extract, get_embeddings, get_loc_embeddings, X_ANY, read_tsv
from elit.util.component import BILOU

__author__ = 'Jinho D. Choi'


class NERState(ForwardState):
    def __init__(self, document, params):
        """
        NERState inherits the one-pass, left-to-right tagging strategy from ForwardState.
        :param document: the input document.
        :type document: elit.nlp.structure.Document
        :param params: parameters created by NERecognizer.create_params()
        :type params: SimpleNamespace
        """
        super().__init__(document, params.label_map, params.zero_output, NER)
        self.windows = params.windows
        self.embs = [get_loc_embeddings(document), get_embeddings(params.word_vsm, document)]

        if params.name_vsm: self.embs.append(get_embeddings(params.name_vsm, document))
        if params.gaze_vsm: self.embs.append(get_embeddings(params.gaze_vsm, document))
        if params.p2v_vsm: self.embs.append(get_embeddings(params.p2v_vsm, document, POS))
        if params.c2v_vsm: self.embs.append(get_embeddings(params.c2v_vsm, document))

        # self.output = [[self.zero_output] * len(s) for s in document]  # null previous prediction
        self.embs.append((self.output, self.zero_output))  # add previous prediction

        if params.ch2v_vsm: self.embs.append(get_embeddings(params.ch2v_vsm, document))

    def eval(self, metric):
        """
        :type metric: elit.nlp.metric.F1
        """
        preds = self.labels

        for i, sentence in enumerate(self.document):
            gold = sentence[NER]
            pred = preds[i]
            # BILOU.quick_fix(pred)

            gold = BILOU.collect(gold)
            auto = BILOU.collect(pred)

            metric.correct += len([1 for k, v in gold.items() if v == auto.get(k, None)])
            metric.p_total += len(gold)
            metric.r_total += len(auto)

    @property
    def x(self):
        """
        :return: the n x d matrix where n = # of windows and d = 2 + word_emb.dim + name_emb.dim + num_class
        """
        t = len(self.document[self.sen_id])
        l = ([x_extract(self.tok_id, w, t, emb[self.sen_id], zero) for w in self.windows] for emb, zero in self.embs)
        return np.column_stack(l)


class NERModel(CNNcharModel):
    def __init__(self, params, **kwargs):
        """
        :param params: parameters to initialize POSModel.
        :type params: SimpleNamespace
        :param kwargs: parameters to initialize gluon.Block.
        :type kwargs: dict
        """
        loc_dim = len(X_ANY)
        word_dim = params.word_vsm.dim
        name_dim = params.name_vsm.dim if params.name_vsm else 0
        gaze_dim = params.gaze_vsm.dim if params.gaze_vsm else 0
        p2v_dim = params.p2v_vsm.dim if params.p2v_vsm else 0
        c2v_dim = params.c2v_vsm.dim if params.c2v_vsm else 0

        char_in = params.ch2v_vsm.dim if params.ch2v_vsm else 0
        char_out = params.ch2v_vsm.out_dim if params.ch2v_vsm else 0

        input_col = loc_dim + word_dim + name_dim + gaze_dim + p2v_dim + c2v_dim + char_out + params.num_class
        ngram_cconv = [SimpleNamespace(filters=f, kernel_row=i, activation='relu') for i, f in
                       enumerate(params.ngram_filters, 1)]
        ngram_wconv = [SimpleNamespace(filters=f, kernel_row=i, activation='relu') for i, f in
                       enumerate(params.ngram_filters, 1)]
        super().__init__(char_in, char_out, input_col, params.num_class, ngram_cconv, ngram_wconv, params.dropout, **kwargs)


class NERModelLR(gluon.Block):
    def __init__(self, params, **kwargs):
        """
        :param params: parameters to initialize POSModel.
        :type params: SimpleNamespace
        :param kwargs: parameters to initialize gluon.Block.
        :type kwargs: dict
        """
        super().__init__(**kwargs)
        self.dropout = gluon.nn.Dropout(params.dropout)
        self.out = gluon.nn.Dense(params.num_class)

    def forward(self, x):
        x = self.dropout(x)
        x = self.out(x)
        return x

class NERecognizer(NLPComponent):
    def __init__(self, ctx, word_vsm, name_vsm=None, gaze_vsm=None, p2v_vsm=None, c2v_vsm=None, ch2v_vsm=None, num_class=17, windows=(-2, -1, 0, 1, 2),
                 ngram_filters=(128, 128, 128, 128, 128), dropout=0.2, label_map=None, model_path=None):
        """
        :param ctx: the context (e.g., CPU or GPU) to process this component.
        :type ctx: mxnet.context.Context
        :param word_vsm: the vector space model for word embeddings.
        :type word_vsm: elit.nlp.lexicon.VectorSpaceModel
        :param name_vsm: the vector space model for ambiguity classes.
        :type name_vsm: elit.nlp.lexicon.VectorSpaceModel
        :param gaze_vsm: #TODO
        :param num_class: the total number of classes to predict.
        :type num_class: int
        :param windows: the contextual windows for feature extraction.
        :type windows: tuple of int
        :param ngram_filters: the number of filters for n-gram convolutions.
        :type ngram_filters: tuple of int
        :param dropout: the dropout ratio.
        :type dropout: float
        :param label_map: the mapping between class labels and their unique IDs.
        :type label_map: elit.nlp.lexicon.LabelMap
        :param model_path: if not None, this component is initialized by objects saved in the model_path.
        :type model_path: str
        """
        if model_path and os.path.isfile(pkl(model_path)):
            f = open(pkl(model_path), 'rb')
            # f = open(model_path, 'rb')
            label_map = pickle.load(f)
            num_class = pickle.load(f)
            windows = pickle.load(f)
            ngram_filters = pickle.load(f)
            dropout = pickle.load(f)
            f.close()

        self.params = self.create_params(word_vsm, name_vsm, gaze_vsm, p2v_vsm, c2v_vsm, ch2v_vsm, num_class, windows, ngram_filters, dropout, label_map)
        super().__init__(ctx, NERModel(self.params))

        if model_path and os.path.isfile(gln(model_path)):
            self.model.load_params(gln(model_path), ctx=ctx)
            logging.info('Load model = %s' % model_path)
        else:
            # print('no load:', model_path)
            ini = mx.init.Xavier(magnitude=2.24, rnd_type='gaussian')
            self.model.collect_params().initialize(ini, ctx=ctx)

    def save(self, filepath):
        f = open(pkl(filepath), 'wb')
        pickle.dump(self.params.label_map, f)
        pickle.dump(self.params.num_class, f)
        pickle.dump(self.params.windows, f)
        pickle.dump(self.params.ngram_filters, f)
        pickle.dump(self.params.dropout, f)
        f.close()

        self.model.save_params(gln(filepath))

    def create_state(self, document):
        return NERState(document, self.params)

    @staticmethod
    def create_params(word_vsm, name_vsm, gaze_vsm, p2v_vsm, c2v_vsm, ch2v_vsm, num_class, windows, ngram_filters, dropout, label_map):
        return SimpleNamespace(
            word_vsm=word_vsm,
            name_vsm=name_vsm,
            gaze_vsm=gaze_vsm,
            p2v_vsm=p2v_vsm,
            c2v_vsm=c2v_vsm,
            ch2v_vsm=ch2v_vsm,
            label_map=label_map or LabelMap(),
            num_class=num_class,
            windows=windows,
            ngram_filters=ngram_filters,
            dropout=dropout,
            zero_output=np.zeros(num_class).astype('float32'))


# ======================================== Train ========================================

def train_args():
    def int_tuple(s):
        return tuple(map(int, s.split(',')))

    def context(s):
        d = int(s[1:]) if len(s) > 1 else 0
        return mx.cpu(d) if s[0] == 'c' else mx.gpu(d)

    parser = argparse.ArgumentParser('Train: named entity recognition')

    # data
    parser.add_argument('-t', '--trn_path', type=str, metavar='filepath', help='path to the training data (input)')
    parser.add_argument('-d', '--dev_path', type=str, metavar='filepath', help='path to the development data (input)')
    parser.add_argument('-ts', '--tst_path', type=str, metavar='filepath', default=None, help='path to the test data (input)')
    parser.add_argument('-m', '--mod_path', type=str, metavar='filepath', default=None, help='path to the model data (output)')
    parser.add_argument('-vt', '--tsv_tok', type=int, metavar='int', default=0, help='the column index of tokens in TSV')
    parser.add_argument('-vp', '--tsv_pos', type=int, metavar='int', default=2, help='the column index of pos-tags in TSV')
    parser.add_argument('-vn', '--tsv_ner', type=int, metavar='int', default=4, help='the column index of ner-label in TSV')

    # lexicon
    parser.add_argument('-wv', '--word_vsm', type=str, metavar='filepath', help='vector space model for word embeddings')
    parser.add_argument('-nv', '--name_vsm', type=str, metavar='filepath', default=None, help='vector space model for named entity gazetteers')
    parser.add_argument('-gd', '--gaze_vsm', type=str, metavar='filepath', default=None, help='directory for entity gazetteers')
    parser.add_argument('-go', '--gaze_option', type=int, metavar='int', default=1, help='vector representation option for entity gazetteer')
    parser.add_argument('-pv', '--p2v_vsm', type=int, metavar='int', default=0, help='dimension for pos2vec')
    parser.add_argument('-cv', '--c2v_vsm', type=str, metavar='filepath', default=None, help='cluster vector embeddings')
    parser.add_argument('-ch', '--ch2v_vsm', type=int, metavar='int', default=0, help='char vectors')


    # configuration
    parser.add_argument('-nc', '--num_class', type=int, metavar='int', default=17, help='number of classes')
    parser.add_argument('-cw', '--windows', type=int_tuple, metavar='int[,int]*', default=(-2, -1, 0, 1, 2), help='contextual windows for feature extraction')
    parser.add_argument('-nf', '--ngram_filters', type=int_tuple, metavar='int[,int]*', default=(128,128,128,128,128), help='number of filters for n-gram convolutions')
    parser.add_argument('-do', '--dropout', type=float, metavar='float', default=0.2, help='dropout')

    parser.add_argument('-cx', '--ctx', type=context, metavar='[cg]\d', default=0, help='device context')
    parser.add_argument('-ep', '--epoch', type=int, metavar='int', default=100, help='number of epochs')
    parser.add_argument('-tb', '--trn_batch', type=int, metavar='int', default=64, help='batch size for training')
    parser.add_argument('-db', '--dev_batch', type=int, metavar='int', default=1024, help='batch size for evaluation')
    parser.add_argument('-lr', '--learning_rate', type=float, metavar='float', default=0.01, help='learning rate')

    args = parser.parse_args()

    log = ['Configuration',
           '- train batch    : %d' % args.trn_batch,
           '- learning rate  : %f' % args.learning_rate,
           '- dropout ratio  : %f' % args.dropout,
           '- n-gram filters : %s' % str(args.ngram_filters),
           '- num of classes : %d' % args.num_class,
           '- windows        : %s' % str(args.windows)]

    logging.info('\n'.join(log))
    return args


def train():
    logging.basicConfig(format='%(message)s', level=logging.INFO)
    mx.random.seed(11)
    random.seed(11)

    # processor
    args = train_args()
    word_vsm = FastText(args.word_vsm)
    name_vsm = Word2Vec(args.name_vsm) if args.name_vsm else None
    gaze_vsm = NamedEntityTree(args.gaze_vsm, args.gaze_option) if args.gaze_vsm else None
    p2v_vsm = Pos2Vec(args.p2v_vsm) if args.p2v_vsm > 0 else None
    c2v_vsm = Cluster2Vec(args.c2v_vsm) if args.c2v_vsm else None
    ch2v_vsm = Char2Vec(args.ch2v_vsm) if args.ch2v_vsm > 0 else None
    comp = NERecognizer(args.ctx, word_vsm, name_vsm, gaze_vsm, p2v_vsm, c2v_vsm, ch2v_vsm, args.num_class, args.windows, args.ngram_filters, args.dropout, model_path=args.mod_path)

    # states
    cols = {TOKEN: args.tsv_tok, NER: args.tsv_ner, POS: args.tsv_pos}
    trn_states = read_tsv(args.trn_path, cols, comp.create_state)
    dev_states = read_tsv(args.dev_path, cols, comp.create_state)
    tst_states = read_tsv(args.tst_path, cols, comp.create_state) if args.tst_path else None
    # optimizer
    loss_func = gluon.loss.SoftmaxCrossEntropyLoss()
    trainer = gluon.Trainer(comp.model.collect_params(), 'adagrad', {'learning_rate': args.learning_rate})

    # train
    best_e, best_eval = -1, -1
    trn_metric = F1()
    dev_metric = F1()
    tst_metric = F1()

    if 'tst' in args.dev_path and False:
        dev_metric.reset()
        dev_eval = comp.evaluate(dev_states, args.dev_batch, dev_metric)
        logging.info('Initial dev-f1 = %5.2f' % dev_eval[0])

    for e in range(args.epoch):
        trn_metric.reset()
        dev_metric.reset()
        tst_metric.reset()

        st = time.time()
        trn_eval = comp.train(trn_states, args.trn_batch, trainer, loss_func, trn_metric)
        mt = time.time()
        dev_eval = comp.evaluate(dev_states, args.dev_batch, dev_metric)
        et = time.time()
        tst_eval = comp.evaluate(tst_states, args.dev_batch, tst_metric) if args.tst_path else None

        if best_eval < dev_eval[0]:
            best_e, best_eval = e, dev_eval[0]
            if args.mod_path:
                comp.save(args.mod_path+'.'+str(e))

        logging.info(
            '%4d: trn-time: %d, dev-time: %d, trn-f1: %5.2f, dev-f1: %5.2f,%s num-class: %d, best-acc: %5.2f @%4d' %
            (e, mt-st, et-mt, trn_eval[0], dev_eval[0], (' tst-f1: %5.2f,' % tst_eval[0] if args.tst_path else ''),
             len(comp.params.label_map), best_eval, best_e))


if __name__ == '__main__':
    train()