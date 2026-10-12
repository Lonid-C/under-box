# BNBU 教师站中间证书

`globalsign-atlas-r3-ov-tls-ca-2026-q2.pem` 是 BNBU 教师站当前证书的中间 CA，签发者为 GlobalSign Root CA R3，有效期至 2028-01-21。SHA-256 指纹：

`8B:C4:73:66:C0:E8:2A:24:9E:E1:A8:76:98:13:B7:BF:D6:71:8D:25:3D:D6:9A:6C:4B:4B:E7:69:33:62:F1:E9`

`app.search._verify_for_url()` 仅对 `staff.bnbu.edu.cn` 补充此证书。它保留系统根证书、证书有效期和主机名校验；没有关闭 TLS 验证。存在有效的 `BNBU_TLS_CA_FILE` 配置时优先使用该配置，否则使用随代码保存的文件，避免网关重启时漏传环境变量造成回归。
