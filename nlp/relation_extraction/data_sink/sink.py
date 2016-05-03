from nlp.relation_extraction.data_sink import MODEL_PATH
from nlp.relation_extraction.data_sink import ELASTIC_HOST,ELASTIC_PORT

from elasticsearch_dsl.connections import connections
from os import listdir
from os.path import isfile, join, abspath
from enum import Enum
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor
from queue import Queue, Full, Empty
from threading import Lock

import importlib, re, logging

logger = logging.getLogger(__name__)
PY_FILE_REGEX = re.compile(".*\.py$")
ModelIdentifier = namedtuple("ModelIdentifier", ['index', 'mapping', 'model_class'])


class SinkLoader:

    def __init__(self):
        self.model_location = abspath(MODEL_PATH)
        self.load_path = ".".join(MODEL_PATH.split("/"))
        self.models = list()
        self.data_sinks = dict()

    def form_sinks(self):
        model_modules = [c for c in listdir(self.model_location) if
                         isfile(join(self.model_location, c)) if c != '__init__.py']

        model_modules = [m for m in model_modules if PY_FILE_REGEX.match(m)]
        for model_module in model_modules:
            # get the name of the class
            model_module = model_module.split('.')[0]
            try:
                module_path = self.load_path + '.' + model_module

                model_class = model_module
                module = importlib.import_module(module_path)
                self.models.append(ModelIdentifier(index=module.index, mapping=module.mapping,
                                                   model_class=module.model_class))
            except (ImportError, Exception) as e:
                raise RuntimeError("Error importing the module ", e)

        for model in self.models:
            model_name = model.index + "." + model.mapping
            connections.create_connection(model_name, hosts=[ELASTIC_HOST], port=ELASTIC_PORT)
            data_sink = ElasticDataSink(model_name, connections.get_connection(model_name), model)
            self.data_sinks[model_name] = data_sink

    def close_sinks(self):
        for data_sink_name in self.data_sinks.keys():
            connections.remove_connection(data_sink_name)


class ElasticDataSink:

    class SinkState(Enum):
        inited = 1
        started = 2
        stopped = 3

    def __init__(self, name, conn, model_identifier, workers=5, bound=10000):
        self.name = name
        self.conn = conn
        self.model_identifier = model_identifier
        self.state = ElasticDataSink.SinkState.inited
        self.pool = ThreadPoolExecutor(max_workers=workers)
        self.queue = Queue(maxsize=bound)
        self.item_lock = Lock()
        self.jobs = set()

    def start(self):
        assert self.state == ElasticDataSink.SinkState.inited, "sink state must be inited "
        self.state = ElasticDataSink.SinkState.started
        self.model_identifier.model_class.init(using = self.conn)

    def stop(self):
        if self.state == ElasticDataSink.SourceState.stopped:
            # already stopped return gracefully, without doing anything
            return
        self.state = ElasticDataSink.SourceState.stopped

        # cancel all the pending future executions on the pool
        for job in list(self.jobs):
            job.cancel()
        self.pool.shutdown(wait=True)

    def __sink_item(self):
        try:
            self.item_lock.acquire()
            item = self.queue.get_nowait()
            self.item_lock.release()

            self.queue.task_done()
            return item.save(using=self.conn)
        except Empty as e:
            # nothing to do here, we didn't find anything
            logger.info("no more elements in the queue ", e)
        except Exception as e:
            raise RuntimeError("Error sinking the item to ES", e)

    def __callback(self, item_future):
        self.jobs.remove(item_future)
        if self.state != ElasticDataSink.SinkState.started:
            return

        if item_future.exception():
            logger.error("Error in future evaluation")
            logger.error(item_future.exception())
        else:
            logger.info("output on future evaluation")
            logger.info(item_future.result())

    def sink_item(self, item):
        assert isinstance(item, self.model_identifier.model_class), \
            " item must be instance of " + self.model_identifier.model_class
        try:
            self.queue.put(item, timeout=10)
            # add a sink item job request
            f = self.pool.submit(self.__sink_item)
            self.jobs.add(f)
            f.add_done_callback(self.__callback)

        except Full as e:
            raise RuntimeError("Error sinking item queue full", e)