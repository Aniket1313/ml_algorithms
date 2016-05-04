import pattern.en as pattern

from nltk.stem import PorterStemmer
from nltk.tokenize import word_tokenize
from practnlptools import tools as pnt
from threading import Thread
from multiprocessing import Pool, Queue, Process, Manager
from queue import Full, Empty


import nlp.relation_extraction.data_source.source as DSource
import nlp.relation_extraction.data_sink.sink as DSink

from nlp.relation_extraction import RelationModifier, RelationArgument, EntityTuple, \
    POS_TAG_ENTITY, PRONOUN_PHRASES, RelationTuple, JOB_LIB_TEMP_FOLDER

import logging, time
logger = logging.getLogger(__name__)


def form_relation(object, item):
    object.form_relations_source(item)


class RelationExtractor:
    """
    Relation Extraction based on Semantic Role Labeling of SENNA
    """
    def __init__(self, data_source=None, relation_sink=None, workers=8):
        """
        :param data_source: data_source object of type DataSource
        :param relation_sink: data_sink object of type DataSink
        :param workers: number of child process workers in source sink mode
        """
        if data_source:
            assert isinstance(data_source, DSource.MongoDataSource),\
                "data_source object must be instance of MongoDataSource"
            self.data_source = data_source

        if relation_sink:
            assert isinstance(relation_sink, DSink.ElasticDataSink), \
                "relation_sink object must be instance of ElasticDataSink"
            self.relation_sink = relation_sink
            self.model_class = self.relation_sink.model_identifier.model_class

        self.relation_annotator = pnt.Annotator()
        self.stemmer = PorterStemmer()
        self.workers = workers
        self.relation_queue = Manager().Queue(maxsize=10000)
        self.persist_attributes = ['relation_annotator', 'stemmer', 'model_class', 'relation_queue']

    def __getstate__(self):
        state = dict()
        for attr in self.persist_attributes:
            state[attr] = self.__dict__[attr]
        return state

    def __setstate(self, d):
        self.__dict__.update(d)

    @staticmethod
    def __normalize_entity(entity, chunk_parse, pos_tags):
        """
        process a entity to form a normalized representation
        :param entity: entity to be normalized, tokenized into words
        :param chunk_parse: chunk parse of the entity
        :param pos_tags: pos tags of the tokens in the entity
        :return: normalized entity, return None if normalization leads
        to no entity been generated
        """

        intermediate_entities, normalized_entities = [], []
        current_entity = EntityTuple(None, [])

        for ((entity_index, entity_token), chunk) in zip(enumerate(entity), chunk_parse):
            chunk_tag = chunk[1]
            if chunk_tag == 'E-NP':
                index = current_entity.index if current_entity.index is not None else entity_index
                current_entity.value.append(entity_token)
                intermediate_entities.append(EntityTuple(index, current_entity.value))
                current_entity = EntityTuple(None, "")

            elif chunk_tag == 'B-NP':
                current_entity = EntityTuple(entity_index, [entity_token])

            elif chunk_tag == 'S-NP':
                intermediate_entities.append(EntityTuple(entity_index, [entity_token]))

            elif chunk_tag == 'I-NP':
                correct_index = current_entity.index if current_entity.index is not None \
                    else entity_index
                current_entity.value.append(entity_token)
                current_entity = EntityTuple(correct_index, current_entity.value)

            elif chunk_tag in PRONOUN_PHRASES:
                if current_entity.index:
                    current_entity.value.append(entity_token)
                    current_entity = EntityTuple(current_entity.index, current_entity.value)

        if current_entity not in intermediate_entities:
            intermediate_entities.append(current_entity)

        for entity_tuple in intermediate_entities:
            entity_text = ""
            for index, entity_token in enumerate(entity_tuple.value):
                if pos_tags[entity_tuple.index + index][1] in POS_TAG_ENTITY:
                    entity_text += (" " + entity_token)
            if entity_text: normalized_entities.append(entity_text.strip())

        if not normalized_entities: return None
        return " ".join(normalized_entities)

    def __normalize_relation(self, relation_phrase):
        """
        normalize a relation verb
        :param relation_phrase: relation verb phrase to be normalized
        :return: normalized form of the verb
        """
        return " ".join([self.stemmer.stem(w) for w in word_tokenize(relation_phrase)])

    @staticmethod
    def __sublist_find(haystack, needle):
        """
        Find a needle in a haystack, aka sublist find in a bigger list
        :param haystack: bigger list
        :param needle: list to find
        :return: return the start index of the smaller list in bigger list,
        -1 if not found
        """
        matched, start_index = 0, -1
        for (hay_index, hay_ele) in enumerate(haystack):
            if hay_ele != needle[matched]:
                matched = 0
                start_index = -1
            else:
                matched += 1
                if start_index == -1: start_index = hay_index
                if matched == len(needle): return start_index

        return start_index

    @staticmethod
    def __populate_arguments(semantic_element):
        """
        form a argument object from the srl semantic element
        :param semantic_element: SRL semantic element
        :return: RelationArgument instance
        """
        return RelationArgument(A0=semantic_element.get('A0'), A1=semantic_element.get('A1'),
                                A2=semantic_element.get('A2'), A3=semantic_element.get('A3'))

    @staticmethod
    def __populate_modifier(semantic_element):
        """
        form a argument modifier object from the srl semantic element
        :param semantic_element: SRL semantic element
        :return: RelationModifier instance
        """
        return RelationModifier(DIR=semantic_element.get('AM-DIR'), MNR=semantic_element.get('AM-MNR'),
                                LOC=semantic_element.get('AM-LOC'), TMP=semantic_element.get('AM-TMP'),
                                EXT=semantic_element.get('AM-EXT'), PNC=semantic_element.get('AM-PNC'),
                                CAU=semantic_element.get('AM-CAU'), NEG=semantic_element.get('AM-NEG'))

    @staticmethod
    def word_tokenize_entity(words, entity):
        """
        Tokenize an entity string using the words in the given word list,
        uses DP to compute optimal complete split of the entity string
        :param words: list of words to tokenize the string by
        :param entity: the string to be tokenized
        :return: return the tokenized entity as list of tokens
        """
        # append white space character to the words list
        words.append(" ")
        entity_char_tokens = [c for c in entity]
        tokenized_entity, split_index = [], []
        cover = [False] * (len(entity_char_tokens))

        for index, entity_c in enumerate(entity_char_tokens):
            assignments = [(j, cover[j] and words.count("".join(entity_char_tokens[j + 1: index + 1])))
                           for j in xrange(index - 1, -1, -1)]

            assignments.append((-1, words.count("".join(entity_char_tokens[0: index + 1]))))
            if not assignments:
                cover[index] = False
            else:
                token_segment = max(assignments, key=lambda e: e[1])
                cover[index] = token_segment[1]
                if cover[index]: split_index.append((index, token_segment[0]))

        end, start = split_index[-1]
        split_index = dict(split_index)
        while True:
            word_found = ''.join(entity_char_tokens[start + 1: end + 1])
            if word_found not in [' ']: tokenized_entity.append(word_found)
            end, start = start, split_index.get(start)
            if not start: break

        words.remove(" ")
        return tokenized_entity[::-1]

    @staticmethod
    def __form_entity(sentence_as_tokens, arg_text, chunk_parse, pos_tags):
        """
        Form a entity from a argument string using the parsed chunks and pos tags
        and the sentence tokenization of the sentence from which the argument is generated
        :param sentence_as_tokens: tokenized sentence generating the argument
        :param arg_text: argument string
        :param chunk_parse: chunking of the argument
        :param pos_tags: pos of the argument
        :return: string of the normalized entity
        """
        tokenized_arg = RelationExtractor.word_tokenize_entity(sentence_as_tokens, arg_text)
        arg_index = RelationExtractor.__sublist_find(sentence_as_tokens, tokenized_arg)

        arg0_chunk_parse = chunk_parse[arg_index: arg_index + len(tokenized_arg)]
        arg0_pos_tag = pos_tags[arg_index: arg_index + len(tokenized_arg)]

        en0 = RelationExtractor.__normalize_entity(tokenized_arg, arg0_chunk_parse, arg0_pos_tag)
        return en0

    def form_relations(self, text, persist=True):
        """
        form relation(s) on a given text
        :param text: text on which to get the relations on,
        text will be sentence tokenized and relations formed at sentence level
        :param persist: persist the relations extracted from the text in the sink,
        relation_sink needed to be specified
        :return: list of relations
        """
        text_sentences = pattern.tokenize(text)
        relations = []
        for sentence in text_sentences:

            # work with ascii string only
            sentence = "".join((c for c in sentence if 0 < ord(c) < 127))
            senna_annotation = self.relation_annotator.getAnnotations(sentence)

            chunk_parse, pos_tags, role_labeling, tokenized_sentence = \
                senna_annotation['chunk'], senna_annotation['pos'], senna_annotation['srl'], \
                senna_annotation['words']

            # nothing to do here empty srl
            if not role_labeling: continue

            for semantic_element in role_labeling:
                arguments = RelationExtractor.__populate_arguments(semantic_element)
                modifiers = RelationExtractor.__populate_modifier(semantic_element)
                verb = semantic_element.get('V')
                # order of the arguments returned is important, A0 --> A1 --> A2 --> A3
                arguments = [v for v in vars(arguments).itervalues() if v]
                modifiers = [v for v in vars(modifiers).itervalues() if v]

                if not arguments: continue
                argument_pairs = [e for e in ((ai, aj) for i, ai in enumerate(arguments) for j, aj
                                              in enumerate(arguments) if i < j)]

                verb = self.__normalize_relation(verb)
                for a0, a1 in argument_pairs:
                    en0 = RelationExtractor.__form_entity(tokenized_sentence, a0, chunk_parse, pos_tags)
                    en1 = RelationExtractor.__form_entity(tokenized_sentence, a1, chunk_parse, pos_tags)
                    if not en0 or not en1: continue
                    relations.append(RelationTuple(left_entity=en0, right_entity=en1, relation=verb,
                                                   sentence=sentence))
                for arg_modifier in modifiers:
                    mod_pos = sentence.find(arg_modifier)
                    linked_arg = min([(a, abs(mod_pos - sentence.find(a))) for a in arguments], key=lambda e: e[1])[0]
                    en0 = RelationExtractor.__form_entity(tokenized_sentence, linked_arg, chunk_parse, pos_tags)
                    en1 = RelationExtractor.__form_entity(tokenized_sentence, arg_modifier, chunk_parse, pos_tags)
                    if not en0 or not en1: continue
                    relations.append(RelationTuple(left_entity=en0, right_entity=en1, relation=verb,
                                                   sentence=sentence))

        return relations

    def form_relations_source(self, source_item):
        if not source_item:
            logger.error("got an empty source item")
            return

        for item_entry in source_item:
            if item_entry == ' ': continue
            try:
                relations = self.form_relations(item_entry)
            except RuntimeError as e:
                logger.error("Error generating relations")
                logger.error(e)
                continue

            for relation in relations:
                sink_relation = self.model_class()
                sink_relation.leftEntity = relation.left_entity
                sink_relation.rightEntity = relation.right_entity
                sink_relation.relation = relation.relation
                sink_relation.text = relation.sentence
                logger.info("generated a relation")
                logger.info(sink_relation)

                try:
                    self.relation_queue.put(sink_relation, timeout=1)
                except Full as e:
                    logger.error(e)

    def sink_relations(self):
        while not self.all_sinked:
            try:
                item = self.relation_queue.get_nowait()
                self.relation_sink.sink_item(item)
            except Empty as e:
                pass

    def form_relations_from_source(self):

        if not self.data_source or not self.relation_sink:
            raise RuntimeError("Data source and sink must be set")

        self.data_source.start()
        self.relation_sink.start()

        self.all_sinked = False
        pool = Pool(processes=self.workers)
        t1 = time.time()
        pool.imap(self.form_relations_source, self.data_source, chunksize=8)

        sinker = Thread(target=self.sink_relations, name='Sink-Thread')
        sinker.start()

        pool.close()
        pool.join()
        self.all_sinked = True
        t2 = time.time()
        logger.info("process finished in :: %d  seconds" %(t2 - t1))







