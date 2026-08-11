"""Module providing a yaml and json parser with pydantic validation"""

from __future__ import annotations

import os
from typing import Any, cast

import yaml

from config_saver.lib.errors import RootRequiredError
from config_saver.lib.models.model import Model
from config_saver.lib.utils.path_expander import PathExpander


class Parser:
    """Class representing a yaml and json parser for our model"""

    def __init__(self, filename: str):
        self.filename: str = filename
        with open(self.filename, encoding="utf-8") as yaml_file:
            yaml_data = yaml.safe_load(yaml_file)

        validated_data = Model.model_validate(yaml_data)

        # Check if only_root_user is enabled and verify user permissions.
        # Note: root user (uid==0) can always execute any configuration.
        if validated_data.only_root_user and os.getuid() != 0:
            raise RootRequiredError(filename)

        raw_dict: dict[str, Any] = validated_data.model_dump()
        expander = PathExpander()
        expanded_dict: dict[str, Any] = self._expand_dict(raw_dict, expander)
        validated_expanded = Model.model_validate(expanded_dict)
        self._model: Model = validated_expanded
        self._data: dict[str, Any] = expanded_dict
        # Paths whose ${BEGINS_WITH=...}/${ENDS_WITH=...} placeholder matched nothing.
        self.unresolved_paths: list[str] = list(expander.unresolved)
        self.ambiguous_paths: list[tuple[str, list[str]]] = list(expander.ambiguities)

    def get_attr(self, attr_name: str) -> Any | None:
        """Get an attribute from the parsed data"""
        return self._data.get(attr_name, None)

    def get_data(self) -> dict[str, Any]:
        """Return the parsed (and already-expanded) data as a dictionary"""
        return self._data

    def _expand_dict(self, data: dict[str, Any], expander: PathExpander) -> dict[str, Any]:
        """Return a copy of data with paths expanded in 'location' and 'directories'."""
        out: dict[str, Any] = data.copy()
        # Expand the 'location' fields under save/export when present.
        for section in ["save", "export"]:
            if section in out:
                for item in out[section]:
                    loc = out[section][item].get("location")
                    if loc:
                        out[section][item]["location"] = expander.expand(loc)
        # Expand the entries of the 'directories' list when present.
        if "directories" in out:
            new_dirs: list[str | dict[str, Any]] = []
            for entry in out["directories"]:
                if isinstance(entry, str):
                    new_dirs.append(expander.expand(entry))
                elif isinstance(entry, dict) and "source" in entry:
                    entry_dict = cast(dict[str, Any], entry)
                    new_entry: dict[str, Any] = entry_dict.copy()
                    new_entry["source"] = expander.expand(str(entry_dict["source"]))
                    new_dirs.append(new_entry)
                elif isinstance(entry, dict):
                    new_dirs.append(cast(dict[str, Any], entry))
                else:
                    new_dirs.append(str(entry))
            out["directories"] = new_dirs
        return out

    def get_model(self) -> Model:
        """Return the validated pydantic Model instance."""
        return self._model
