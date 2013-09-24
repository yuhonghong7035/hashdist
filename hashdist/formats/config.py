"""
Handles reading the Hashdist configuration file. By default this is
``~/.hashdist/config.yaml``.
"""

import os
from os.path import join as pjoin
from ..deps import jsonschema
from .marked_yaml import (load_yaml_from_file, validate_yaml, ValidationError)

DEFAULT_CONFIG_FILENAME_REPR = '~/.hashdist/config.yaml'
DEFAULT_CONFIG_FILENAME = os.path.expanduser(DEFAULT_CONFIG_FILENAME_REPR)

config_schema = {
    "$schema": "http://json-schema.org/draft-04/schema#",
    "title": "Hashdist configuration file schema",
    "type": "object",
    "properties": {
        "build_stores": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "dir": {"type": "string"}
                },
                "required": ["dir"]                
            },
            "minItems": 1
        },

        "source_caches": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    # programatically we require one or the other of these
                    "url": {"type": "string"},
                    "dir": {"type": "string"},
                }
            },
            "minItems": 1
        },

        "build_temp": {"type": "string"},
        "cache": {"type": "string"},
        "db": {"type": "string"},
    },
    "required": ["build_stores", "source_caches", "build_temp", "cache", "db"]
}

def _make_abs(cwd, path):
    if not os.path.isabs(path):
        return os.path.realpath(os.path.join(cwd, path))
    else:
        return path

def load_config_file(filename):
    basedir = os.path.dirname(os.path.realpath(filename))
    doc = load_yaml_from_file(filename)
    validate_yaml(doc, config_schema)
    for entry in doc['build_stores']:
        entry['dir'] = _make_abs(basedir, entry['dir'])
        
    for entry in doc['source_caches']:
        if sum(['url' in entry, 'dir' in entry]) != 1:
            raise ValidationError(entry.start_mark, 'Exactly one of "url" and "dir" must be specified')
        if 'dir' in entry:
            entry['dir'] = _make_abs(basedir, entry['dir'])
    for key in ['build_temp', 'cache', 'db']:
        doc[key] = _make_abs(basedir, doc[key])
    return doc

def get_config_example_filename():
    return pjoin(os.path.dirname(__file__), 'config.example.yaml')
