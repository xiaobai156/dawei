# 大围数据_修复版2 项目专属规则

本项目继承上级 `..\AGENTS.md`。本文件只定义“大围36码”项目的专属契约；未写事项按上级统一规则执行。

## 1. 数据契约

- 每站每期结果必须恰好包含36个互不重复的两位数字，合法范围为 `01` 至 `49`。
- 候选识别、缓存、判重和正式输出必须保留页面原始数字顺序；禁止排序、重排或去位置化。
- 一期相同只在期号相同且36个数字的值与位置全部一致时成立。
- `section_keywords` 必须在同一栏目锚点上下文全部命中；`keywords` 必须在目标期上下文全部命中。
- `search_window` 是单站文本扫描边界，不是方向候选数量，禁止混用这两个概念。

## 2. 方向与候选窗口

- 正式方向字段是 `region`，只允许 `top` 或 `bottom`；旧字段 `position=tail` 仅作兼容，不能覆盖 `region`。
- `top` 选择目标区块中原始位置最靠前的合格候选，`bottom` 选择原始位置最靠后的合格候选。
- 当同一文档的候选超过30组（31组及以上），或存在重复期号候选时，启用靠 `top`/`bottom` 的最近5组高可信候选严格窗口。
- 指定期的全部高可信候选必须先执行同期冲突检查；不能先用5组窗口隐藏窗口外的同期冲突。
- 未显式指定期数的重复检测，先按每站 `region` 确定该站最新候选，再以全站多数最新期为基准生成近10期窗口；禁止直接取全页最大期号。
- 超出全站多数最新期窗口的异常期数必须排除，不得用于抓取成功、更新缓存或通过判重，并写入失败TXT或隔离审核报告。

## 3. 正式入口与文件

| 用途 | 项目入口 |
| --- | --- |
| 单期日常抓取 | `爬虫-每天大围网站数字.bat` → `scrape_all_36.py` → `dawei.cli.single_issue` |
| 多期抓取 | `爬虫-大围多期抓取不刷新缓存.bat` → `dawei.cli.multi_issue` |
| 正式判重 | `爬虫-每天大围网站数字重复.bat` → `detect_duplicate_sites.py` → `dawei.cli.duplicate` |
| 失败站独立验证 | `validate_failed_sites_36.py` → `dawei.cli.validate_failed` |

- 活跃站点配置：`sites_36.json`。
- `sites_36.json` 是唯一正式站点清单；文件缺失或无效时必须失败，禁止回退旧内置清单。
- 近10期正式缓存：`近10期重复检测备份.json`。
- 失败正文诊断缓存目录：`cache\`，不得作为成功数据来源。
- V1基准清单：`V1_BASELINE_SHA256.txt`，迁移或修复验收时不得无授权改写。
- 正式TXT目录：`C:\Users\Administrator\Desktop\每天工具\数据系列\大围杀号生肖数据统一归纳`。
- JSON、页面快照及其他中间文件只能留在项目或隔离审核目录，不能写入正式TXT目录。
- 正式单期抓取、内部正式校验和最终验收的联网范围都只能是用户指定期；禁止额外联网抓上一期、下一期或其他相邻期。

正式输出名称：

- 单期成功：`<期数>期-大围-成功.txt`。
- 单期失败：`<期数>期-大围-失败.txt`。
- 重复报告：`<期数>期重复网站.txt`。
- 判重失败：`<期数>期重复检测失败.txt`。
- 多期全失败汇总：`多期-<期数列表>-大围-全部失败汇总.txt`。
- 正式重复报告必须区分“重复组”和“疑似组”，并逐组列出双方站名、URL、按具体期号对齐的重复期数、连续相同期数，以及每一期36码是否数值与位置完全一致。

## 4. 新增站点的项目级硬门

- 大围候选站必须取得缓存近10期窗口内完整的10期有效36码；本项目不适用“历史不足10期仍加入”的普通特例。
- 10期内还必须命中缓存最新期或上一期中的至少一期。
- 正式缓存标记为 `incomplete` 或含失败记录时，禁止新增站点判重和入库。
- 已有站点在缓存中不足10期时按实际有效共同期号参与比较，不因此删除；该规则不能用于放宽候选新站的完整10期门槛。
- 候选JSON必须使用 `--use-backup --candidate-site <路径>` 进入正式检测器；禁止实时重抓全站代替正式缓存基准。
- 身份检查除名称、URL和topic外，还必须覆盖 `site_id`、`api_url` 与动态文章 `record_id`。
- 通过后写入的站点必须明确保存 `site_id`、`source_type`、`parser_id`、`region`、`section_keywords`、`keywords` 和必要的动态来源字段。
- `search_window`、`min_numbers_per_line`、`numbers_before_issue`、`drop_zero_numbers`、`api_url`、`render_policy` 等站点专属参数只有在真实页面证据和对应测试支持时才能启用。

## 5. 专属解析与动态来源

- 当前解析注册表包括：`generic_36`、`three_rows`、`xiongchumo`、`fenfatuqiang`、`xueqiu`、`baoma_xuanji`、`zhuchiren_weite`、`zhuchiren_baote`、`xiaoyuer`、`meirenyu`、`renjianrenai`、`fengkuang_zhongma`、`topic_content_3x12`、`marker_after_issue_3x12`、`section_3x12`、`yiyiba`、`kunnan_magazine`。
- 当前来源类型包括：`generic_html`、`topic_page`、`bbs_topic`、`forum_thread`、`dynamic_article`、`dynamic_collection`。
- 三行式36码只能由 `three_rows` 或对应专属解析器读取连续的三组12码结构，不能由通用36码解析替代。
- `kunnan_magazine` 必须配置专属 `api_url`；缺少接口时直接失败。
- `/article/admin/<记录ID>` 等动态详情页可能实际使用 `/api/proxy/manager-articles/<相同记录ID>`；配置必须保留相同记录ID，禁止根据接口族名称猜测或改用其他文章。
- 动态记录的标题、作者、正文、36码、`record_id` 和 `record_path` 必须来自同一个接口对象。
- 专属解析器ID及其栏目算法由配置和注册表共同锁定，修复时不得改成 `generic_36` 规避专属失败。

## 6. 缓存专属规则

- 单期成功后只合并本次指定期，并将窗口裁剪为 `N-9` 至 `N`；禁止为补缓存再次联网抓相邻期。
- 全量单期存在部分失败时，成功站的真实结果可以写入缓存，但缓存必须设置 `incomplete: true` 并保存全部失败明细。
- 较旧的历史期运行不得使缓存最新期回滚。
- 缓存记录必须保留原始36码顺序及 `record_id`、`source_path`、`raw_position`、`parser_id`。
- 同一期号码不同，或双方都有 `record_id` 但ID不同，必须报缓存冲突。
- 同一期 `record_id` 相同且36码原始顺序相同，仅 `source_path` 表示或 `raw_position` 漂移时允许刷新元数据。
- 缺少 `record_id` 时继续比较来源路径，路径不同不得自动覆盖。
- 正式单期流程先原子写成功/失败TXT，再更新缓存；缓存更新失败仍须返回失败状态，但不得吞掉已完成的TXT。
- 多期入口不得更新 `近10期重复检测备份.json`。

## 7. 项目级严格验收

- 新增或修复必须通过真实指定期的完整单期入口；只调用解析函数、正则或模拟正文不算完成。
- 动态站验收必须覆盖URL记录ID与接口记录ID一致；三行、跨行、标题正文分离、旧文档及同期冲突场景按受影响解析器补测。
- 缓存写入验收必须使用缓存副本；隔离审核前后核对正式 `sites_36.json`、正式缓存和正式TXT未被改写。
- 代码验收命令固定包括：
  - `python -m compileall -q .`
  - `python -m unittest discover -p "test_*.py"`
  - `ruff check .`
- 影响V1迁移契约时，还必须核对 `V1_BASELINE_SHA256.txt` 及V1只读基准哈希。
