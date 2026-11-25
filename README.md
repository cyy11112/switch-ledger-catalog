# 掌机账本公共游戏目录

本仓库发布供掌机账本读取的公共 Nintendo Switch / Switch 2 游戏资料。
数据入口是 [catalog/games.json](catalog/games.json)，每天香港时间 11:17 由 GitHub Actions 检查更新。
每次运行的 Summary 会记录新增、变更和保留记录数量。只有资料有变化才写入新快照。

- [TitleDB](https://github.com/blawar/titledb) 提供公开地区 SKU 元数据，使用 [MIT 许可证](catalog/licenses/titledb.txt)。
- [Wikidata](https://www.wikidata.org/wiki/Wikidata:Data_access/en) 提供 CC0 的 Switch 2 平台声明和可核实的发行日期。介质和地区缺少信息时标为未核实。
- [Switch 2 Tracker](https://github.com/codemaverick-hub/switch2-tracker) 提供用于人工核查的候选。个别已核实版本及来源保存在 catalog/verified-games.json。
- 图片引用由原权利人提供。以 covers/ 开头的路径指向 APP 内置参考图。

本仓库只包含公共目录、构建脚本及相关测试。校验通过后工作流才提交快照。
