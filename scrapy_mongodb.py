# coding:utf-8
"""
scrapy-mongodb - MongoDB pipeline for Scrapy

Homepage: https://github.com/sebdah/scrapy-mongodb
Author: Sebastian Dahlgren <sebastian.dahlgren@gmail.com>
License: Apache License 2.0 <http://www.apache.org/licenses/LICENSE-2.0.html>

Copyright 2013 Sebastian Dahlgren

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""
import datetime
import logging

from pymongo import errors
from pymongo.mongo_client import MongoClient
from pymongo.mongo_replica_set_client import MongoReplicaSetClient
from pymongo.read_preferences import ReadPreference

from scrapy.exporters import BaseItemExporter

VERSION = '0.9.1'


def not_set(string):
    """ Check if a string is None or ''

    :returns: bool - True if the string is empty
    """
    if string is None:
        return True
    elif string == '':
        return True
    return False


class MongoDBPipeline(BaseItemExporter):
    """ MongoDB pipeline class """

    def __init__(self, **kwargs):
        super(MongoDBPipeline, self).__init__(**kwargs)
        self.logger = logging.getLogger('scrapy')
        # Default options
        self.config = {
            'uri': 'mongodb://localhost:27017',
            'fsync': False,
            'write_concern': 0,
            'database': 'scrapy-mongodb',
            'collection': 'items',
            'replica_set': None,
            'unique_key': None,
            'each_collection': False,
            'buffer': None,
            'append_timestamp': False,
            'stop_on_duplicate': 0,
        }

        # Item buffer
        self.current_item = 0
        self.item_buffer = []

        # Duplicate key occurence count
        self.duplicate_key_count = 0

    def load_spider(self, spider):
        self.crawler = spider.crawler
        self.settings = spider.settings

        # Versions prior to 0.25
        if not hasattr(spider, 'update_settings') and hasattr(spider, 'custom_settings'):
            self.settings.setdict(spider.custom_settings or {}, priority='project')

    def open_spider(self, spider):
        self.load_spider(spider)

        # Configure the connection
        self.configure(spider=spider)

        if self.config['replica_set'] is not None:
            connection = MongoReplicaSetClient(
                self.config['uri'],
                replicaSet=self.config['replica_set'],
                w=self.config['write_concern'],
                fsync=self.config['fsync'],
                read_preference=ReadPreference.PRIMARY_PREFERRED)
        else:
            # Connecting to a stand alone MongoDB
            connection = MongoClient(
                self.config['uri'],
                fsync=self.config['fsync'],
                read_preference=ReadPreference.PRIMARY)

        # Set up the database
        self.database = connection[self.config['database']]
        self.collections = {'default': self.database[self.config['collection']]}
        self.config['collection'], self.collection = self.get_collection(spider.name)
        self.logger.info(u'Connected to MongoDB {0}, using "{1}/{2}"'.format(
            self.config['uri'],
            self.config['database'],
            self.config['collection']))

        # Ensure unique index
        if self.config['unique_key']:
            self.collection.ensure_index(self.config['unique_key'], unique=True)
            self.logger.info(u'Ensuring index for key {0}'.format(
                self.config['unique_key']))

        # Get the duplicate on key option
        if self.config['stop_on_duplicate']:
            tmpValue = self.config['stop_on_duplicate']
            if tmpValue < 0:
                self.logger.error(
                    u'Negative values are not allowed for'
                    u' MONGODB_STOP_ON_DUPLICATE option.'
                )
                raise SyntaxError(
                    (
                        'Negative values are not allowed for'
                        ' MONGODB_STOP_ON_DUPLICATE option.'
                    )
                )
            self.stop_on_duplicate = self.config['stop_on_duplicate']
        else:
            self.stop_on_duplicate = 0

    def configure(self, spider):
        """ Configure the MongoDB connection """

        # Set all regular options
        options = [
            ('uri', 'MONGODB_URI'),
            ('fsync', 'MONGODB_FSYNC'),
            ('write_concern', 'MONGODB_REPLICA_SET_W'),
            ('database', 'MONGODB_DATABASE'),
            ('collection', 'MONGODB_COLLECTION'),
            ('replica_set', 'MONGODB_REPLICA_SET'),
            ('each_collection', 'MONGODB_EACH_COLLECTION'),
            #('unique_key', 'MONGODB_UNIQUE_KEY'),
            ('buffer', 'MONGODB_BUFFER_DATA'),
            ('append_timestamp', 'MONGODB_ADD_TIMESTAMP'),
            ('stop_on_duplicate', 'MONGODB_STOP_ON_DUPLICATE')
        ]

        MONGODB_UNIQUE_KEY = self.settings.get('MONGODB_UNIQUE_KEY', '')
        if not not_set(MONGODB_UNIQUE_KEY):
            if isinstance(MONGODB_UNIQUE_KEY, dict):
                self.config['unique_key'] = MONGODB_UNIQUE_KEY.get(spider.name, None)
            else:
                self.config['unique_key'] = MONGODB_UNIQUE_KEY
            self.logger.debug('config["unique_key"] value is {}'.format(self.config['unique_key']))

        for key, setting in options:
            if not not_set(self.settings[setting]):
                self.config[key] = self.settings[setting]

        # Check for illegal configuration
        if self.config['buffer'] and self.config['unique_key']:
            self.logger.error(
                u'IllegalConfig: Settings both MONGODB_BUFFER_DATA '
                u'and MONGODB_UNIQUE_KEY is not supported'
            )
            raise SyntaxError(
                (
                    u'IllegalConfig: Settings both MONGODB_BUFFER_DATA '
                    u'and MONGODB_UNIQUE_KEY is not supported'
                ))

    def get_collection(self, name):
        if self.config['each_collection']:
            collection = self.collections.get(name)
            collection_name = name
            if not collection:
                collection = self.database[name]
                self.collections[name] = collection
        else:
            collection = self.collections.get('default')
            collection_name = self.config['collection']

        # Ensure unique index
        if self.config['unique_key']:
            collection.ensure_index(self.config['unique_key'], unique=True)
            self.logger.debug(u'Ensuring index for key {0}'.format(
                self.config['unique_key']))
        return (collection_name, collection)

    def process_item(self, item, spider):
        """ Process the item and add it to MongoDB

        :type item: Item object
        :param item: The item to put into MongoDB
        :type spider: BaseSpider object
        :param spider: The spider running the queries
        :returns: Item object
        """
        item = dict(self._get_serialized_fields(item))

        if self.config['buffer']:
            self.current_item += 1

            if self.config['append_timestamp']:
                item['scrapy-mongodb'] = {'ts': datetime.datetime.utcnow()}

            self.item_buffer.append(item)

            if self.current_item == self.config['buffer']:
                self.current_item = 0
                return self.insert_item(self.item_buffer, spider)

            else:
                return item

        return self.insert_item(item, spider)

    def close_spider(self, spider):
        """ Method called when the spider is closed

        :type spider: BaseSpider object
        :param spider: The spider running the queries
        :returns: None
        """
        if self.item_buffer:
            self.insert_item(self.item_buffer, spider)

    def insert_item(self, item, spider):
        """ Process the item and add it to MongoDB

        :type item: (Item object) or [(Item object)]
        :param item: The item(s) to put into MongoDB
        :type spider: BaseSpider object
        :param spider: The spider running the queries
        :returns: Item object
        """
        if not isinstance(item, list):
            item = dict(item)

            if self.config['append_timestamp']:
                item['scrapy-mongodb'] = {'ts': datetime.datetime.utcnow()}

        #collection_name, collection = self.get_collection(spider.name)

        if self.config['unique_key'] is None:
            try:
                self.collection.insert(item, continue_on_error=True)
                self.logger.debug(
                    u'Stored item(s) in MongoDB {0}/{1}'.format(
                        self.config['database'], self.config['collection'])
                )
            except errors.DuplicateKeyError:
                self.logger.debug(u'Duplicate key found')
                if (self.stop_on_duplicate > 0):
                    self.duplicate_key_count += 1
                    if (self.duplicate_key_count >= self.stop_on_duplicate):
                        self.crawler.engine.close_spider(
                            spider,
                            'Number of duplicate key insertion exceeded'
                        )
                pass

        else:
            key = {}
            if isinstance(self.config['unique_key'], list):
                for k in dict(self.config['unique_key']).keys():
                    key[k] = item[k]
            else:
                key[self.config['unique_key']] = item[self.config['unique_key']]

            self.collection.update(key, item, upsert=True)

            self.logger.debug(
                u'Stored item(s) in MongoDB {0}/{1}'.format(
                    self.config['database'], self.config['collection'])
            )

        return item
