import re
js = open("miniapp/need/app_v77.js").read()
html = open("miniapp/index.html").read()
ids = set(re.findall(r"document\.getElementById\(['\"](.*?)['\"]\)", js))
html_ids = set(re.findall(r"id=['\"](.*?)['\"]", html))
missing = ids - html_ids
print("Missing IDs:", missing)
