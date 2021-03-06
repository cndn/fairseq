# Copyright (c) 2017-present, Facebook, Inc.
# All rights reserved.
#
# This source code is licensed under the license found in the LICENSE file in
# the root directory of this source tree. An additional grant of patent rights
# can be found in the PATENTS file in the same directory.

import os

from collections import OrderedDict

from fairseq import tokenizer
from fairseq.data.masked_lm_dictionary import MaskedLMDictionary

from fairseq.data import (
    IndexedCachedDataset,
    IndexedDataset,
    IndexedRawTextDataset,
    TokenBlockDataset,
)

from fairseq.data import Dictionary
from fairseq.data.masked_lm_dataset import MaskedLMDataset
from fairseq.data.multi_corpus_sampled_dataset import MultiCorpusSampledDataset

from . import FairseqTask, register_task


@register_task('cross_lingual_lm')
class CrossLingualLMTask(FairseqTask):
    """
    Task for training cross-lingual language models.
    For more details look at: https://arxiv.org/pdf/1901.07291.pdf
    Args:
        dictionary (Dictionary): the dictionary for the input of the task
    """

    @staticmethod
    def add_args(parser):
        """Add task-specific arguments to the parser."""
        parser.add_argument('data', help='path to data directory')
        parser.add_argument('--tokens-per-sample', default=512, type=int,
                            help='max number of total tokens over all segments'
                                 ' per sample')
        parser.add_argument('--monolingual-langs', default='en', type=str,
                            help='comma separated list of languages for which we'
                                 ' want to train XLM on')
        parser.add_argument('--raw-text', default=False, action='store_true',
                            help='load raw text dataset')
        parser.add_argument('--lazy-load', action='store_true',
                            help='load the dataset lazily')
        parser.add_argument('--shuffle', action='store_true',
                            help='shuffle each monolingual dataset while'
                            ' training')

    def __init__(self, args, dictionary):
        super().__init__(args)
        self.dictionary = dictionary
        self.seed = args.seed
        self.distributed_world_size = args.distributed_world_size
        self.langs2id = self._lang_to_id(args.monolingual_langs)
        self.default_key = None

    def _lang_to_id(
            self,
            languages: str
    ):
        """
        Build a map from languages to ids. These ids are used as segment labels
        for cross-lingual LM training.
        """
        lang2id = {}
        langs = [l.strip() for l in languages.split(',')]
        for id, lang in enumerate(langs):
            lang2id[lang] = id
        return lang2id


    @classmethod
    def load_dictionary(cls, filename):
        return MaskedLMDictionary.load(filename)

    @classmethod
    def build_dictionary(cls, filenames, workers=1, threshold=-1, nwords=-1, padding_factor=8):
        d = MaskedLMDictionary()
        for filename in filenames:
            Dictionary.add_file_to_dictionary(filename, d, tokenizer.tokenize_line, workers)
        d.finalize(threshold=threshold, nwords=nwords, padding_factor=padding_factor)
        return d

    @property
    def target_dictionary(self):
        return self.dictionary

    @classmethod
    def setup_task(cls, args, **kwargs):
        """Setup the task.
        """
        dictionary = MaskedLMDictionary.load(os.path.join(args.data, 'dict.txt'))

        print('| dictionary: {} types'.format(len(dictionary)))

        return cls(args, dictionary)

    def load_dataset(self, split, combine=False):
        """Load a given dataset split.
        Args:
            split (str): name of the split (e.g., train, valid, test)
        """
        dataset_map = OrderedDict()

        for lang in self.langs2id.keys():
            if self.default_key is None:
                self.default_key = lang
            # Datasets are expected to be in "split.lang" format (Eg: train.en)
            language_split = '{}.{}'.format(split, lang)
            path = os.path.join(self.args.data, language_split)

            if self.args.raw_text and IndexedRawTextDataset.exists(path):
                ds = IndexedRawTextDataset(path, self.dictionary)
            elif not self.args.raw_text and IndexedDataset.exists(path):
                if self.args.lazy_load:
                    ds = IndexedDataset(path, fix_lua_indexing=True)
                else:
                    ds = IndexedCachedDataset(path, fix_lua_indexing=True)
            else:
                raise FileNotFoundError('Dataset not found: {} ({})'.format(
                    language_split, self.args.data))

            # Since we append each block with the classification_token,
            # we need to effectively create blocks of length
            # tokens_per_sample-1
            block_dataset = TokenBlockDataset(
                dataset=ds,
                sizes=ds.sizes,
                block_size=self.args.tokens_per_sample-1,
                pad=self.dictionary.pad(),
                eos=self.dictionary.eos()
            )

            dataset_map[lang] = MaskedLMDataset(
                dataset=block_dataset,
                sizes=block_dataset.sizes,
                vocab=self.dictionary,
                pad_idx=self.dictionary.pad(),
                mask_idx=self.dictionary.mask(),
                classif_token_idx=self.dictionary.eos(),
                sep_token_idx=self.dictionary.eos(),
                shuffle=getattr(self.args, 'shuffle', False),
                has_pairs=False,
                segment_id=self.langs2id[lang],
                seed=self.seed,
            )

        self.datasets[split] = MultiCorpusSampledDataset(
            dataset_map, default_key=self.default_key
        )
        print('| {} {} {} examples'.format(
            self.args.data, split, len(self.datasets[split])
            )
        )
