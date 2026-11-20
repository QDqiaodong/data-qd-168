原创虚构合成样例，MIT许可。无真实客户、出行记录、测量成果或他人书籍正文。

- input.json：v1 资源编译样例（en/fr/fr-CA 问候与购物车文案）。
- history_input.json：v2 审核历史样例（g 键的 en 源修订 s1→s2、fr 译文 t1→t2、批准与撤销，及六个联动查询）。
- release_input.json：v3 发布包样例（共享 shared 组件的菱形依赖，应用 app-a 依赖 checkout/catalog，app-b 仅依赖 admin）。
- workqueue_input.json：v4 派工台账样例（greeting 键的源修订 s1→s2、fr 译文 t1 的批准/撤销/再批准，app-a 经 checkout 与 catalog 双路径引用 shared，app-b 经 b-ui 引用；五个查询覆盖各阶段截止）。
- vendor_workspace.json / vendor_selection.json：v5 离线供应商交接样例（de/ja 目标语言，同语境三键共用一组、同文异语境单独成组、重复变量槽位与字面花括号、无槽位空译文；选择行故意乱序以演示确定性）。
- delivery_input.json：v6 离线发布交付样例（fr 译文 t1 经回退供给 app-a/app-b 两个 fr-CA 包，app-c 独立；t1 的批准 seq 4 被撤销后以 seq 10 重新获准；cart 的获准 fr 译文为空串；五个查询覆盖四个发布号及一个只改无关项的候选版）。
