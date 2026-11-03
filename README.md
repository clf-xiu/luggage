# 一起飞 / 行李智囊 —— Render 全云端部署

前端、后端、数据库全部运行在 Render，不再依赖本机 MySQL 或 Live Server，可 24 小时独立运行。

```
浏览器 ──HTTPS──> Render Web Service (gunicorn + Flask)
                        │ DATABASE_URL（Render 内网）
                        ▼
                 Render PostgreSQL (free)
```

## 1. 文件说明

| 路径 | 说明 |
| --- | --- |
| `contral/app.py` | 应用实现，`app = Flask(...)` |
| `app.py` | 根目录兼容入口，`from contral.app import app` |
| `contral/seed_data.py` | 种子数据：航司 21 条、物品 86 条 |
| `ui/all.html` | 旅客端，`GET /` |
| `ui/后台.html` | 后台管理，`GET /admin` |
| `data/baggage_sql.mysql.sql` | 原 MySQL 建表脚本，**仅作历史资料**，云端不再使用 |
| `requirements.txt` | 依赖固定版本（含 `psycopg2-binary`） |
| `Procfile` / `render.yaml` | Render 启动与 Blueprint 配置 |
| `.gitignore` | 排除 `.venv`、`.idea`、截图、密钥 |

## 2. 数据库模式

`get_conn()` 为双模式：

| 条件 | 行为 |
| --- | --- |
| 设置了 `DATABASE_URL` | PostgreSQL（psycopg2 + `RealDictCursor`），云端主模式 |
| 未设置 `DATABASE_URL` | 回退本机 MySQL（pymysql），仅用于本地开发 |

- 云端**只需要一个环境变量** `DATABASE_URL`（Render 的 Internal Database URL），
  代码会把 `postgres://` 自动规范化为 `postgresql://`。
- 本地 MySQL 回退使用 `DB_HOST`/`DB_PORT`/`DB_USER`/`DB_PASSWORD`/`DB_NAME`，
  默认 `127.0.0.1:3306`、库 `travel_baggage`，密码不带默认值，仓库内不含任何密码。
- 接口字段名与返回结构未变，前端无需修改。

## 3. 启动自动建表 + 灌种子

应用启动时会执行 `init_db()`（仅 PostgreSQL 模式）：

1. `CREATE TABLE IF NOT EXISTS` 建 `airline_weight`、`item_kb`，主键为 `SERIAL PRIMARY KEY`；
2. 表为空时导入 `contral/seed_data.py`：航司 7 家 × 3 舱位 = 21 条，物品 86 条；
3. 插入使用 `ON CONFLICT DO NOTHING`，不手写 id，重复启动不会重复数据；
4. 初始化失败只打日志，不影响应用启动。

因此 Render 首次部署完成后打开页面就有数据，不需要去 PSQL 控制台手敲 SQL。

## 4. 启动命令

`app.py` 实际位于 `contral/`，因此：

```bash
# 推荐写法（render.yaml / Procfile 使用）
gunicorn contral.app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120

# 借助根目录兼容入口，也可用
gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120
```

健康检查：`GET /healthz` 返回 `{"ok": true}`（只表示进程存活，不探测数据库）。

## 5. Blueprint 部署流程（推荐）

1. 把仓库推送到 GitHub（见第 7 节；不要提交 `.venv`、密码）。
2. 打开 Render Dashboard → **New** → **Blueprint**。
3. 选择该仓库，Render 读取根目录 `render.yaml`。
4. 确认要创建：
   - PostgreSQL：`yiqifei-db`（free，库名 `travel_baggage`）
   - Web Service：`yiqifei-baggage`（free，Python）
   - `DATABASE_URL` 通过 `fromDatabase` 自动注入，**不需要手工填写**。
5. 点 **Apply**，等待首次构建完成。
6. 验证顺序：`/healthz` → `/api/test`（应显示 86 条）→ `/` → `/admin`。

之后数据库地址由 Render 管理，重建/迁移都不用改代码或环境变量。

## 6. 本地开发

本地没有 Render PostgreSQL 时，走 MySQL 回退（可选）：

```powershell
$env:DB_HOST="127.0.0.1"; $env:DB_PORT="3306"; $env:DB_USER="root"
$env:DB_PASSWORD="你的本机 MySQL 密码"; $env:DB_NAME="travel_baggage"

.\.venv\Scripts\python.exe contral\app.py
```

访问 <http://127.0.0.1:5000/> 与 <http://127.0.0.1:5000/admin>。
本地 MySQL 为空库时，可手动导入历史脚本（仅首次；启动自动建表只对 PostgreSQL 生效）：

```powershell
& "C:\Program Files\MySQL\MySQL Server 8.0\bin\mysql.exe" -u root -p travel_baggage `
  < data\baggage_sql.mysql.sql
```

## 7. 推送到 GitHub

> 当前机器未检测到 `git` 命令，因此暂存操作未能执行。安装 Git 后在项目根目录运行：

```powershell
git init
git add .gitignore README.md requirements.txt Procfile render.yaml app.py `
  contral/app.py contral/seed_data.py ui/all.html "ui/后台.html" `
  "ui/favicon(1).png" data/baggage_sql.mysql.sql
git status
git commit -m "deploy: Render PostgreSQL + auto init"
git remote add origin https://github.com/<你的账号>/<仓库名>.git
git push -u origin main
```

`.gitignore` 已排除 `.venv`、`.idea`、截图目录、`*.zip`、`.env`、密钥，
不会把本地环境或旧压缩包推上去。然后在 Render 用 Blueprint 连接该仓库。

## 8. 免费额度与限制

- Render 免费 Web Service **约 15 分钟无请求会休眠**，下一次访问为冷启动（几十秒）。
- Render 免费 PostgreSQL 有**有效期限制（90 天，以控制台为准）**，到期数据库会被删除，
  数据不会保留；课程演示结束后请及时导出，或升级付费实例。
- 免费数据库容量与连接数有限，适合演示规模；`gunicorn` 固定 1 worker + 4 threads。
- 免费实例无自动备份，重要数据请自行导出。
- 前端 TensorFlow.js 从 jsDelivr CDN 加载；CDN 不可达时识图模型会停在“加载中”，
  不影响重量查询与物品问答。
- 后端 AI 识图依赖 torch，未列入依赖（免费实例内存也不足），
  `/api/image/recognize` 会返回“AI 识图暂未启用”的降级提示。

## 9. `DATABASE_URL` 缺失时的降级

- 应用仍能启动：`/`、`/admin`、`/healthz` 正常。
- 数据库类接口返回中文友好提示（HTTP 200 + `success/found: false`），不会崩溃。
- 云端部署时 `DATABASE_URL` 由 `render.yaml` 自动注入，正常不会缺失；
  若手动删除了变量，重新 Deploy 即可恢复。
- 若本地设置了无效的 PostgreSQL 连接串，`init_db()` 只告警，接口按错误返回。

## 10. 本地验证结果

- `py_compile` 通过；`import contral.app`、`import app` 均正常。
- `postgres://` → `postgresql://` 规范化逻辑单测通过。
- `GET /`、`GET /admin`、`GET /healthz`、`/favicon(1).png` 均 200，两个页面内容不同。
- 实际启动 `python contral/app.py` 进程，HTTP 请求 `/healthz`、`/`、`/api/test` 均正常返回。
- 未设置 `DATABASE_URL` 时：启动不崩溃，数据库接口返回友好错误（本地 MySQL 未提供密码）。
- 设置无效的 PostgreSQL 连接串时：`init_db()` 只告警，进程启动与 `/healthz` 不受影响。
- `psycopg2` 可解析规范化后的 DSN（识别出 user/host/port/dbname/password）。
- 种子数据：`AIRLINE_WEIGHT_SEED` 21 条（每行 3 字段、航司+舱位唯一），
  `ITEM_KB_SEED` 86 条（每行 6 字段、物品名唯一），无超出 VARCHAR(255) 的字段。
- PostgreSQL DDL / INSERT（含 `ON CONFLICT`）语句用 PostgreSQL 语法解析器校验通过。
- **未验证**：真实 PostgreSQL 连接与建表灌数（本地没有 PostgreSQL 服务）、
  Render 实际部署（缺账号与仓库授权）、gunicorn 实际启动（Windows 不支持 gunicorn）。
