# 代码扫描修复小工具

用纯 Python 3 标准库编写，无需安装任何第三方包，可在 Windows / macOS / Linux 上使用。

把本工具指向一个 `.py` 文件或目录，它会扫描常见问题并给出「文件:行号」报告。加上 `--fix` 后，会自动修复其中比较安全的那一类，并在改写前生成 `.bak` 备份。

## 用法

在本文件夹中打开终端（Windows 可用 PowerShell 或命令提示符）：

```text
python code_fixer.py 示例_有问题的代码.py
python code_fixer.py --dry-run 示例_有问题的代码.py
python code_fixer.py --fix 示例_有问题的代码.py
python code_fixer.py --fix --check-print --report report.json 示例_有问题的代码.py
python code_fixer.py 某个目录
```

如果系统里用 `py` 启动器，把上面的 `python` 换成 `py` 即可。

常用参数：

| 参数 | 含义 |
| --- | --- |
| `--fix` | 自动修复安全项，默认先写 `.bak` 备份 |
| `--dry-run` | 只预览将要修改的 diff，不写文件 |
| `--no-backup` | 修复时不创建备份（请谨慎） |
| `--report FILE` | 把结果写成 JSON 报告 |
| `--max-line N` | 行长度限制，默认 120；设为 0 可关闭 |
| `--check-print` | 额外检查可能是调试残留的 `print()` |

## 会检查什么

- 语法错误
- 行尾多余空白
- Tab / 空格混合缩进（含同一行内混合）
- 文件末尾缺少换行符
- 未使用的导入（基于 AST，简单情况可自动删除）
- 裸 `except:`
- 使用 `== None` / `!= None`（应写成 `is None` / `is not None`）
- 可变对象作为函数默认参数（如 `def f(x=[])`，只报告不自动改）
- 过长行（默认超过 120 个字符，只报告不自动改）
- 同一文件混用 CRLF / LF 换行（只报告不自动改）
- 可选：调试残留的 `print()`（需加 `--check-print`，只报告不自动改）

## 会自动修复什么（需 `--fix`）

- 去掉行尾空白
- 把缩进中的 Tab 换成空格（按 Tab 宽度 4）
- 在文件末尾补上换行符
- `== None` 改为 `is None`，`!= None` 改为 `is not None`
- 裸 `except:` 改为 `except Exception:`
- 删除或改写未使用的导入（只处理单行、无分号的简单情况；带 `# noqa` 的行会跳过）

不会自动改的问题：语法错误、可变默认参数、过长行、混合换行符、`print()`。这些需要你自己判断。

## 建议的试用步骤

1. 先扫描示例文件，看报告：`python code_fixer.py --check-print 示例_有问题的代码.py`
2. 再预览修复：`python code_fixer.py --dry-run 示例_有问题的代码.py`
3. 确认无误后真正修复：`python code_fixer.py --fix 示例_有问题的代码.py`
4. 原文件会留下来，备份是 `示例_有问题的代码.py.bak`

递归扫描时会自动跳过 `__pycache__`、`.git`、`venv`、`.venv` 等目录。
