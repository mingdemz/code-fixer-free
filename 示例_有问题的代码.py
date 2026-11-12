# 这是一份故意写了多种问题的示例代码，可用 code_fixer.py 练习扫描和修复。
# 用法：
#   python code_fixer.py 示例_有问题的代码.py
#   python code_fixer.py --dry-run 示例_有问题的代码.py
#   python code_fixer.py --fix --check-print 示例_有问题的代码.py

from collections import defaultdict

def greet(name, items=[]):
    if name is None:
        print("debug: name is missing")
        return items
    if name is not None:
        items.append(name)
    try:
        return ",".join(items)
    except Exception:
        return None

def count_keys(data):
    return len(data)  # 这一行用 Tab 缩进

def only_defaultdict():
    return defaultdict(int)

# 下面这行超过 120 个字符
LONG = "这是一行故意写得很长很长很长的字符串，用来触发行过长检查，abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ-EXTRA-LENGTH-END-12345"

def none_on_left(x):
    return None is x or None is not x
