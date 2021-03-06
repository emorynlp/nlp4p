# ========================================================================
# Copyright 2018 ELIT
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
# -*- coding:utf-8 -*-
# Author：ported from PyTorch implementation of flair: https://github.com/zalandoresearch/flair to MXNet
# Date: 2018-09-19 15:12

import mxnet as mx

from elit.component.tagger.corpus import NLPTaskDataFetcher
from elit.component.tagger.embeddings import WordEmbeddings, CharLMEmbeddings, StackedEmbeddings
from elit.util.mx import mxnet_prefer_gpu
from elit.component.tagger.sequence_tagger_model import SequenceTagger
from elit.component.tagger.sequence_tagger_trainer import SequenceTaggerTrainer
from elit.resources.pre_trained_models import EN_LM_FLAIR_FW_WMT11, EN_LM_FLAIR_BW_WMT11

if __name__ == '__main__':
    data_folder = 'data/dat'
    # data_folder = 'data/wsj-pos/debug'

    # get training, test and dev data
    columns = {0: 'text', 1: 'pos'}
    corpus = NLPTaskDataFetcher.fetch_column_corpus(data_folder,
                                                    columns,
                                                    train_file='en-pos.trn',
                                                    test_file='en-pos.tst',
                                                    dev_file='en-pos.dev',
                                                    # train_file='train.tsv',
                                                    # test_file='dev.tsv',
                                                    # dev_file='dev.tsv',
                                                    )

    # 2. what tag do we want to predict?
    tag_type = 'pos'

    # 3. make the tag dictionary from the corpus
    tag_dictionary = corpus.make_tag_dictionary(tag_type=tag_type)
    print(tag_dictionary.idx2item)
    model_path = 'data/model/pos/jumbo'
    with mx.Context(mxnet_prefer_gpu()):
        train = False
        print(train)
        use_crf = True
        if train:
            embedding_types = [
                WordEmbeddings(('fasttext', 'crawl-300d-2M-subword')),
                CharLMEmbeddings(EN_LM_FLAIR_FW_WMT11),
                CharLMEmbeddings(EN_LM_FLAIR_BW_WMT11),
            ]

            embeddings = StackedEmbeddings(embeddings=embedding_types)
            # 5. initialize sequence tagger
            tagger = SequenceTagger(hidden_size=256,
                                    embeddings=embeddings,
                                    tag_dictionary=tag_dictionary,
                                    tag_type=tag_type,
                                    use_crf=use_crf)
            # 6. initialize trainer
            trainer = SequenceTaggerTrainer(tagger, corpus, test_mode=False)

            # 7. start training
            trainer.train(model_path, learning_rate=0.1, mini_batch_size=32, max_epochs=100,
                          embeddings_in_gpu=False)
        tagger = SequenceTagger.load(model_path)
        trainer = SequenceTaggerTrainer(tagger, corpus, test_mode=True)
        print(trainer.evaluate(corpus.test, evaluation_method='accuracy', embeddings_in_gpu=False))
