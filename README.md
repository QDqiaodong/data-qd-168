# data-qd-168 · 多语言资源构建

已实现可离线运行的业务闭环，字段、算法约定和未支持边界详见 [数据契约](docs/contract.md)。样例均为自制合成数据，来源及许可见 fixtures/SOURCES.md。

## 独立安装

Python >=3.11（本批实测3.12.14），标准库运行与unittest测试。使用已有uv；setuptools/wheel精确锁定。每个仓库建立独立环境，不共享site-packages。

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-build.lock
uv pip install --python .venv/bin/python --no-build-isolation --no-deps .
uv pip check --python .venv/bin/python
.venv/bin/python -m unittest discover -s tests -v
```

已有公共缓存时可为两条uv pip install添加 --offline；缓存缺失时需先准备构建工具，不伪称离线成功。修改源码后重新安装。

## 端到端演示

在仓库根目录执行。run/为忽略的运行输出，演示不访问网络、不启动服务或容器。

```sh
.venv/bin/locale-bundle --source fixtures/input.json --output run/report.json
```

预期：fr-CA greeting来源fr，cart来源fr-CA；缺译回退诊断1条。

输入JSON最多8MB，拒绝重复键和非有限数；完整验证成功后原子替换JSON报告。输入输出不得相同，拒绝符号链接输出；失败退出2且保留旧报告。同输入重跑结果一致。

## 离线审核历史构建

`compile_reviewed_history(data)` 与 `locale-bundle-history` 在事实日志（源修订、译文修订、审核决定）之上按查询重建"当时获知的审核决定"，契约详见 [数据契约](docs/contract.md) 的 v2 部分。

```sh
.venv/bin/locale-bundle-history --source fixtures/history_input.json --output run/history.json
```

预期：6个视图按查询顺序输出——(at10,seq3) fr-CA 从 fr 取 t1；(at10,seq4) 源升到 s2 后旧译文 stale_source，回退 en；(at10,seq5) 新译文 pending 仍回退 en；(at10,seq6) 批准后从 fr 取 t2；(at12,seq7) 撤销后 rejected 回退 en；(at10,seq7) 撤销尚不可见，仍取 t2。

设计：事实只追加、校验一次；每个查询从按 key / (locale,key) / translation_id 建立的事实索引中，用纯谓词 `effective<=at 且 seq<=known_seq` 过滤后取 `(effective,seq)` 最大者，独立推导当前源修订、当前译文与审核状态，再沿单父回退链编译。不维护跨查询的物化派生状态。维护代价：迟到事实只是新行，无需拆分或重写任何生效区间；撤销就是普通的 `approved=false` 行，由"最新可见审核"规则自然生效；源版本失效按查询即时比较 `source_id` 得出，没有需要回填的批准矩阵；旧 `known_seq` 截止永远可重放。代价是每查询 O(事实数) 扫描（上限 100 查询 × 10000 事实）。备选方案"版本化生效区间"可让单查询更快，但每条迟到事实都要分裂区间并使已派生状态失效，撤销与旧截止重放的维护成本更高；两种方案必须产出相同结果，本实现选择逐查询推导。

单操作者本地批处理；不承诺并发发布或断电恢复。代码/样例采用MIT许可。

## 按应用依赖闭包发布

`compile_release_bundles(data)` 与 `locale-bundle-release` 为共享组件仓中的每个应用生成自包含发布包，契约详见 [数据契约](docs/contract.md) 的 v3 部分。

```sh
.venv/bin/locale-bundle-release --source fixtures/release_input.json --output run/release.json
```

预期：`app-a`（根组件 checkout、catalog 共同依赖 shared）仅含 greeting/button/cart/key-x；key-x 在 fr-CA 下从 fr 解析一次，source_locale 为 fr，included_by 仅 shared，且仅一条回退诊断。`app-b`（根组件 admin）只含 settings，不含 key-x，其 fr-CA 从 en 回退。

语义：应用包键集合 = 各根组件 `requires` 的传递闭包内所有组件声明的键并集；多路径到达或跨应用共享的组件与键在单个应用内只计一次，但一个应用的键不会泄漏到另一个应用的包。每个键仍按 v1 单父链逐键解析并记录 source_locale，回退诊断只覆盖该应用实际包含的键；每个消息与诊断都带 included_by，即闭包内直接声明该键的组件 id（去重排序）。组件依赖图必须无环，所有 id、依赖、根与键引用必须有效，组件的键必须存在于默认 catalog。全部输入和全部应用/语言包验证构建成功后才原子替换输出；失败退出 2 并保留旧输出，8MB 上限、重复 JSON 键/非有限数拒绝、输入输出区分与符号链接拒绝均与 v1 相同。

## 离线派工台账

`locale-bundle-work-queue` 为本地化负责人维护单操作者派工台账，保存在指定的本地 JSON 文件中，契约详见 [数据契约](docs/contract.md) 的 v4 部分。四个子命令：`sync`（按选定查询截止同步）、`assign`（分派译者）、`confirm`（负责人确认完成）、`query`（查询台账）。派工操作不修改审核事实或发布包。

```sh
.venv/bin/locale-bundle-work-queue sync --source fixtures/workqueue_input.json --ledger run/ledger.json --query q1
.venv/bin/locale-bundle-work-queue assign --ledger run/ledger.json --locale fr --key greeting --source-id s1 --translator alice
.venv/bin/locale-bundle-work-queue sync --source fixtures/workqueue_input.json --ledger run/ledger.json --query q2
.venv/bin/locale-bundle-work-queue confirm --ledger run/ledger.json --locale fr --key greeting --source-id s1 --confirmer lead --translation-id t1 --review-seq 3
.venv/bin/locale-bundle-work-queue query --ledger run/ledger.json
```

预期：q1 同步为 fr/greeting/s1 建一项待办（影响应用 app-a、app-b）；分派给 alice；q2 截止看到 t1 获批准入待确认；负责人凭查询所得证据 (t1, 3) 确认完成。

语义：同步输入 = v2 审核历史 + v3 结构的 components/applications（应用列表是本次完整范围，可以为空；组件键必须存在于源事实中）。每个应用按根组件传递闭包计算实际使用的键，尚未在选定截止内生效的键不派工；语言范围是 requested 中的非默认语言，即使能回退到别的语言也检查该语言自己的译文是否获准。一个待办对应一个 (locale, key, source_id)，多路径引用或跨应用共享只建一项并记录影响应用集合；每次同步显式选择一个 query_id，其 at/known_seq 不得小于台账最后接受的值，任一回退拒绝且台账不变。同一查询、截止、范围及任务相关事实均无变化的重跑（含重排等价输入）不追加审计、不建任务、不覆盖人工动作；只有有变化的成功同步才记录所用查询和截止。已有待办的译文获准时进入待确认，负责人凭查询所得的 translation_id、review_seq 确认；撤销会重开同一任务并保留译者与旧确认审计，旧批准证据不能确认后来重新获准的译文。源修订被替换时归档旧任务（保留负责人与确认审计），新修订未获准则新建未分派任务；对旧修订或退出范围任务的人工操作一律拒绝。首个成功的同步可以创建台账；坏输入、坏台账或被拒绝的操作退出 2 且旧文件保持原样。

完整连续样例（应用退出/加回、撤销重开、修订替换归档）可离线运行：

```sh
.venv/bin/python examples/workqueue_demo.py
```
