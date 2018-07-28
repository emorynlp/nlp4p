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
import abc
import bisect
import glob
import inspect

from .structure import Document, Sentence

__author__ = 'Jinho D. Choi'


# ======================================== Structure ========================================

def group_states(data, create_state, max_len=-1):
    """
    Groups sentences into documents such that each document consists of multiple sentences and the total number of words
    across all sentences within a document is close to the specified maximum length.

    :param data: a list of documents.
    :type data: list of elit.structure.Document
    :param create_state: a function that takes a document and returns a state.
    :type create_state: Document -> elit.nlp.component.NLPState
    :param max_len: the maximum number of words; if max_len < 0, it is inferred by the length of the longest sentence.
    :type max_len: int
    :return: list of states, where each state roughly consists of the max_len number of words.
    :rtype: list of elit.nlp.NLPState
    """
    def aux(i):
        ls = d[keys[i]]
        t = ls.pop()
        document.sentences.append(t)
        if not ls: del keys[i]
        return len(t)

    # key = length, value = list of sentences with the key length
    assert isinstance(data, list) and data and isinstance(data[0], Document)
    d = {}

    for doc in data:
        for sen in doc.sentences:
            d.setdefault(len(sen), []).append(sen)

    keys = sorted(list(d.keys()))
    if max_len < 0:
        max_len = keys[-1]

    states = []
    document = Document()
    wc = max_len - aux(-1)

    while keys:
        idx = bisect.bisect_left(keys, wc)
        if idx >= len(keys) or keys[idx] > wc:
            idx -= 1
        if idx < 0:
            states.append(create_state(document))
            document = Document()
            wc = max_len - aux(-1)
        else:
            wc -= aux(idx)

    if document:
        states.append(create_state(document))

    return states


# ======================================= Evaluation Metric ========================================

class EvalMetric(abc.ABC):
    @abc.abstractmethod
    def update(self, document: Document):
        """
        Resets all counts to 0.
        """
        pass

    @abc.abstractmethod
    def get(self):
        """
        :return: the evaluated score.
        """
        return


class Accuracy(EvalMetric):
    def __init__(self):
        super(Accuracy, self).__init__()
        self.correct = 0
        self.total = 0

    def get(self):
        """
        :rtype: float
        """
        return 100.0 * self.correct / self.total


class F1(EvalMetric):
    def __init__(self):
        super(F1, self).__init__()
        self.correct = 0
        self.p_total = 0
        self.r_total = 0

    def get(self):
        """
        :return: (F1 score, prediction, recall)
        :rtype: (float, float, float)
        """
        p = 100.0 * self.correct / self.p_total
        r = 100.0 * self.correct / self.r_total
        f1 = 2 * p * r / (p + r)
        return f1, p, r


# ======================================== File ========================================

def pkl(filepath):
    return filepath + '.pkl'


def gln(filepath):
    return filepath + '.gln'


def tsv_reader(filepath, args):
    documents = []
    wc = sc = 0

    for filename in glob.glob(filepath):
        sentences, tokens, tags = [], [], []
        fin = open(filename)

        for line in fin:
            if line.startswith('#'): continue
            l = line.split()
            if l:
                tokens.append(l[args.tok])
                tags.append(l[args.pos])
            elif len(tokens) > 0:
                wc += len(tokens)
                sentences.append(Sentence(tok=tokens, pos=tags))
                tokens, tags = [], []

        fin.close()
        sc += len(sentences)
        documents.append(Document(sen=sentences))

    print('Reading: dc = %d, sc = %d, wc = %d' % (len(documents), sc, wc))
    return documents


def json_reader(filepath, args):
    # TODO: to be filled
    documents = []
    return documents


# ======================================== More ========================================

def get_default_args(func):
    signature = inspect.signature(func)
    return {
        k: v.default
        for k, v in signature.parameters.items()
        if v.default is not inspect.Parameter.empty
    }
