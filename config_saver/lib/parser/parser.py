"""Module providing a yaml and json parser with pydantic validation"""
from typing import Any, Dict, List, Optional, Union, cast

import yaml

from config_saver.lib.models.model import Model
from config_saver.lib.utils.path_expander import PathExpander


class Parser:
    """Class representing a yaml and json parser for our model"""
    def __init__(self, filename: str):
        self.filename: str = filename
        with open(self.filename, "r", encoding="utf-8") as yaml_file:
            yaml_data = yaml.safe_load(yaml_file)
            validated_data = Model.model_validate(yaml_data)
            raw_dict: Dict[str, Any] = validated_data.model_dump()
            expanded_dict: Dict[str, Any] = self._expand_dict(raw_dict)
            validated_expanded = Model.model_validate(expanded_dict)
            self._model: Model = validated_expanded
            self._data: Dict[str, Any] = expanded_dict

    def get_attr(self, attr_name: str) -> Optional[Any]:
        """Get an attribute from the parsed data"""
        return self._data.get(attr_name, None)

    def get_data(self) -> Dict[str, Any]:
        """Return the parsed (and already-expanded) data as a dictionary"""
        return self._data

    def _expand_dict(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Return a copy of data with paths expanded in 'location' and 'directories'."""
        expander = PathExpander()
        out: Dict[str, Any] = data.copy()
        # Expande los campos 'location' en save/export si existen
        for section in ["save", "export"]:
            if section in out:
                for item in out[section]:
                    loc = out[section][item].get("location")
                    if loc:
                        out[section][item]["location"] = expander.expand(loc)
        # Expande los valores en la lista 'directories' si existe
        if "directories" in out:
            new_dirs: List[Union[str, Dict[str, Any]]] = []
            for entry in out["directories"]:
                if isinstance(entry, str):
                    new_dirs.append(expander.expand(entry))
                elif isinstance(entry, dict) and "source" in entry:
                    # Cast entry to Dict[str, Any] to help type checker
                    entry_dict = cast(Dict[str, Any], entry)
                    new_entry: Dict[str, Any] = entry_dict.copy()
                    new_entry["source"] = expander.expand(str(entry_dict["source"]))
                    new_dirs.append(new_entry)
                else:
                    # Cast unknown entry to appropriate type
                    if isinstance(entry, dict):
                        new_dirs.append(cast(Dict[str, Any], entry))
                    else:
                        new_dirs.append(str(entry))
            out["directories"] = new_dirs
        return out

    def get_model(self) -> Model:
        """Return the validated pydantic Model instance."""
        return self._model
