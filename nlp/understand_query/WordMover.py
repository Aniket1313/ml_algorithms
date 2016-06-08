from __future__ import division

import sys, re
import logging
from multiprocessing import Pool, Manager
from collections import Counter
from itertools import chain
import numpy as np

from nltk.corpus import stopwords
from nltk.stem import PorterStemmer
from practnlptools import tools
from hashlib import sha256
from pyemd import emd

from nlp.sense2vec import sense_tokenize
from nlp.sense2vec import SenseEmbedding as SE
from nlp.understand_query import QueryTiler as QT
from sklearn.metrics.pairwise import euclidean_distances

logger = logging.getLogger(__name__)

reload(sys)
sys.setdefaultencoding('utf8')

alpha_numeric = re.compile('^\w+$')

class WordMoverModel:
    """
    word mover model for information retrieval on bag of vectors model
    using distance computation on queries and documents using an upper bound approximation
    of Earth Mover Distance

    reference : http://jmlr.org/proceedings/papers/v37/kusnerb15.pdf
    """
    def __init__(self, data_source, workers, embedding, alpha=0.7):
        self.source = data_source
        self.workers = workers
        self.tokenized_blocks = Manager().list()
        self.annotator = tools.Annotator()
        self.stemmer = PorterStemmer()
        self.stop_words = set(stopwords.words('english'))
        assert isinstance(embedding, SE.SenseEmbedding), "embedding must be instance of SenseEmbedding"
        self.embedding = embedding
        self.alpha = alpha
        self.document_models = {}
        self.query_tiler = QT.QueryTiler(self.embedding)

    def tokenize(self, block_input):
        block_id, text_block = block_input
        sense_phrases = sense_tokenize(text_block, self.annotator, self.stemmer, self.stop_words)
        self.tokenized_blocks.append((block_id, sense_phrases, text_block))

    def _filter_word(self, word_sense):
        word = word_sense.split("|")[0]
        if word in self.embedding.stop_words:
            return False
        return True if re.match(alpha_numeric, word) else False

    def form_distance_matrix(self):
        self.vocab = [w for w in self.embedding.model.vocab if self._filter_word(w)]
        self.vocab = {w: index for index, w in enumerate(self.vocab)}
        self.reverse_vocab = {index: w for index, w in enumerate(self.vocab)}
        words = sorted(self.vocab.iteritems(), key=lambda e: e[1])
        word_vectors = [self.embedding.model[w].astype(np.float64) for w,_ in words
                        if self.vocab.has_key(w)]
        self.distance_matrix = euclidean_distances(word_vectors)

    def vectorize(self, words):
        vector_words = np.zeros(len(self.vocab))
        p_indices = [self.vocab[w] for w in words]
        vector_words[p_indices] = 1
        return vector_words

    def form_doc_bags(self):
        for block_id, sense_phrases, block in self.tokenized_blocks:
            sense_words = set(chain(*sense_phrases))
            # ignore oov words of the documents, ideally this should not happen
            # if the model is trained on the document data
            sense_words = [w for w in sense_words if self.vocab.has_key(w)]
            if sense_words:
                self.document_models[block_id] = (sense_words, block)

    @staticmethod
    def label_distance(word1, word2):
        l1 = word1.split("|")[1]
        l2 = word2.split("|")[1]
        return 0 if l1 == l2 else 1

    def assign_nearest_nbh(self, query_doc):

        block_id, query_words, doc_words = query_doc
        query_vector = self.vectorize(query_words)
        doc_vector = self.vectorize(doc_words)
        #distance = emd(query_vector, doc_vector, self.distance_matrix)
        #return block_id, distance

        doc_indices = np.nonzero(doc_vector)[0]
        query_indices = np.nonzero(query_vector)[0]

        doc_centroid = np.average([self.embedding.model[self.reverse_vocab[i]] for i in doc_indices], axis=0)
        query_centroid = np.average([self.embedding.model[self.reverse_vocab[i]] for i in query_indices], axis=0)

        dist_arr = np.array([[(self.distance_matrix[w_i, q_j] + self.distance_matrix[q_j, w_i]) / 2
                              for w_i in doc_indices] for q_j in query_indices])

        label_assignment = np.argmin(dist_arr, axis=1)
        label_assignment = [(index, l) for index, l in enumerate(label_assignment)]

        distances = [dist_arr[l] for l in label_assignment]
        distance = (1 - self.alpha) * np.sum(distances) + \
                   self.alpha * np.linalg.norm(doc_centroid - query_centroid, ord=2)
        return block_id, distance

    def word_mover_distance(self, query, tiled=False, topn=10):
        """
        get the set of documents nearest to the query , as bag of senses EMD
        :param query: query as string will be white space tokenized
        :param topn: number of nearest documents to return, avoid more than 10
        :return: [(block_id, block)] for the topn nearest document neighbours of the query
        """
        if not tiled:
            tiled_words = self.query_tiler.tile(query, include_stopwords=True)[0]
        else:
            tiled_words = query

        if not tiled_words: raise RuntimeError("query could not be tiled by the embedding model")
        tiled_words = [w for w in tiled_words if self.vocab.has_key(w)]

        #uniq_query_tokens = Counter(tiled_words)
        #token_counts = [uniq_query_tokens[t] / len(tiled_words) for t in tiled_words]
        candidates = [(block_id, tiled_words, doc_words) for block_id, (doc_words, _)
                      in self.document_models.iteritems()]
        #pool = Pool(processes=4)
        neighbours = map(self.assign_nearest_nbh, candidates)
        #pool.close()
        #pool.join()
        #for block_id, (sense_words, block) in self.document_models.iteritems():
        #    distance = self.__assign_nearest_nbh(block_id, tiled_words, sense_words)
        #    if distance < 40: print "-----------------", block
        #    neighbours.append((block_id, block, distance))

        neighbours = sorted(neighbours, key=lambda e: e[1])
        return neighbours[:topn]

    def form_model(self):
        self.form_distance_matrix()
        text_blocks = []
        self.source.start()

        logger.info("Reading the text blocks from the source")
        for item_tuple in self.source:
            if not item_tuple:
                logger.warn("item read from source is empty")
                continue
            for f_name, f_value in item_tuple:
                if f_value == '': continue
                item_id = sha256(f_value).hexdigest()
                text_blocks.append((item_id, f_value))

        logger.info("Read all the text blocks")
        logger.info("Number of text blocks read : %d" % len(text_blocks))
        logger.info("will sentence and word tokenize the text blocks")

        pool = Pool(processes=self.workers)
        pool.map(self.tokenize, text_blocks, chunksize=self.workers)
        pool.close()
        pool.join()
        self.form_doc_bags()



