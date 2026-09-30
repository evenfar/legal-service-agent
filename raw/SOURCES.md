本目录存放语料管线原始输入（公有领域法律文本）。

- minfadian_full.txt: 民法典三编官方全文（合同/婚姻家庭/侵权责任，共700条），
  整理自开源结构化数据并对照政府公开文本核验；ingest_laws 的直接输入。
- civil-code.json: 民法典结构化元数据（官方发布信息，作版本依据保留）。
- precedents_raw.md: 最高法指导性案例37件（民事选编），
  来源与逐案官网链接见该文件头部来源清单。

生成物: app/agent/rag/knowledge/*.md（由 python -m app.scripts.ingest_laws 产出）。
本目录中的中间下载物（zip/html）已在入库后清理。
