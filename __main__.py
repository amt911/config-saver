from sys import argv
from lib.json_parser.json_parser import JsonParser


a = JsonParser(argv[1])

print(a.debug())
