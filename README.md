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
