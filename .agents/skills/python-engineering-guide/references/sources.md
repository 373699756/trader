# 来源、版本与许可证

## 选材说明

本 skill 于 2026-07-15 从 GitHub 下载资料并做中文归纳。星数仅用于证明选材时的社区关注度，不等于技术结论；星数会持续变化。规范内容优先采用仍维护的英文上游，中文翻译只用于术语交叉核对。

## 上游来源

| 仓库 | 选材时星数 | 固定版本 | 用途 |
| --- | ---: | --- | --- |
| [google/styleguide](https://github.com/google/styleguide) | 39,441 | [`1809c769`](https://github.com/google/styleguide/blob/1809c769de31ba388c755ad15dd057a9ba8531fd/pyguide.md) | Python 语言与代码风格主规范 |
| [realpython/python-guide](https://github.com/realpython/python-guide) | 29,695 | [`0538ef8f`](https://github.com/realpython/python-guide/tree/0538ef8f8cc86fa1fc17911dc8831ad193b06d17) | 工程结构、测试、日志、文档、CI 与发布思路 |
| [zh-google-styleguide/zh-google-styleguide](https://github.com/zh-google-styleguide/zh-google-styleguide) | 10,977 | [`c1f8ac91`](https://github.com/zh-google-styleguide/zh-google-styleguide/tree/c1f8ac9138bce1b829fdffcfa03e8df44584ef22/google-python-styleguide) | 中文术语核对；不覆盖较新的英文规则 |
| [pypa/sampleproject](https://github.com/pypa/sampleproject) | 5,243 | [`621e4974`](https://github.com/pypa/sampleproject/tree/621e4974ca25ce531773def586ba3ed8e736b3fc) | 现代 `pyproject.toml`、`src` 布局、测试与构建样例 |

下载并检查的主要文件包括：Google `pyguide.md`；Real Python 的 `structure.rst`、`style.rst`、`tests.rst`、`logging.rst`、`documentation.rst`、`packaging.rst`、`ci.rst`；中文 Google 指南的语言和风格规则；PyPA Sample Project 的 `pyproject.toml`、`noxfile.py` 和 CI/目录结构。

## 现代化处理

Real Python 指南包含仍有价值的模块化、测试和文档原则，也包含面向旧 Python/旧打包生态的建议。本 skill 明确不继承以下内容：

- 不把 `setup.py`、`easy_install`、Python 2 或旧版 distutils 当作新工程默认方案。
- 不照搬“包不得放入 `src`”的旧观点；使用 PyPA 样例和项目现实决定布局。
- 不固定 Google 内部的 80 列、特定 linter 或公司流程；优先服从仓库 formatter 与 CI。
- 不把任何架构模式设为必选项；按当前复杂度和故障模式选择。

工具、Python 版本和打包规范会更新。涉及具体版本、配置键或安全建议时，应再次查阅对应项目的当前官方文档。

## 许可证与署名

- Google Style Guide 使用 [Creative Commons Attribution 3.0](https://github.com/google/styleguide/blob/1809c769de31ba388c755ad15dd057a9ba8531fd/LICENSE)。本 skill 对其规则进行了重新组织、中文归纳和项目化调整。
- Real Python Python Guide 使用 [CC BY-NC-SA 3.0](https://github.com/realpython/python-guide/blob/0538ef8f8cc86fa1fc17911dc8831ad193b06d17/LICENSE)。本 skill 仅作带署名的非逐字归纳；若在商业场景分发衍生内容，应单独评估该许可证。
- 中文 Google 开源项目风格指南使用 [Apache-2.0](https://github.com/zh-google-styleguide/zh-google-styleguide/blob/c1f8ac9138bce1b829fdffcfa03e8df44584ef22/google-python-styleguide/LICENSE)。
- PyPA Sample Project 使用 [MIT License](https://github.com/pypa/sampleproject/blob/621e4974ca25ce531773def586ba3ed8e736b3fc/LICENSE.txt)。

本 skill 不是上述项目的官方发布物，也不替代各项目的完整规范。
