"""Module providing a yaml and json parser with pydantic validation"""
from config_saver.lib.utils.path_expander import PathExpander
import yaml
from config_saver.lib.models.model import Model


class Parser:
    """Class representing a yaml and json parser for our model"""
    def __init__(self, filename: str):
        self.filename = filename
        # Let exceptions propagate to the caller for handling (no exit()/print here)
        with open(self.filename, "r", encoding="utf-8") as yaml_file:
            yaml_data = yaml.safe_load(yaml_file)
            validated_data = Model.model_validate(yaml_data)
            # store the validated data, then expand variables and re-validate
            raw_dict = validated_data.model_dump()
            expanded_dict = self._expand_dict(raw_dict)
            # re-validate the expanded dict to produce a Model with expanded paths
            validated_expanded = Model.model_validate(expanded_dict)
            self._model = validated_expanded
            self._data = expanded_dict

    def get_attr(self, attr_name: str):
        """Get an attribute from the parsed data"""
        return self._data.get(attr_name, None)

    def get_data(self):
        """Return the parsed (and already-expanded) data as a dictionary"""
        return self._data

    def _expand_dict(self, data: dict) -> dict:
        """Return a copy of data with paths expanded in 'location' and 'directories'."""
        expander = PathExpander()
        out = data.copy()
        # Expande los campos 'location' en save/export si existen
        for section in ["save", "export"]:
            if section in out:
                for item in out[section]:
                    loc = out[section][item].get("location")
                    if loc:
                        out[section][item]["location"] = expander.expand(loc)
        # Expande los valores en la lista 'directories' si existe
        if "directories" in out:
            new_dirs = []
            for entry in out["directories"]:
                if isinstance(entry, str):
                    new_dirs.append(expander.expand(entry))
                elif isinstance(entry, dict) and "source" in entry:
                    new_entry = entry.copy()
                    new_entry["source"] = expander.expand(entry["source"])
                    new_dirs.append(new_entry)
                else:
                    new_dirs.append(entry)
            out["directories"] = new_dirs
        return out

    def get_model(self) -> Model:
        """Return the validated pydantic Model instance."""
        return self._model
