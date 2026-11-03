from flask import Flask, request, jsonify, send_from_directory
import io
import os
import jieba

# ---- PostgreSQL 驱动（云端主模式）----
# 未安装时应用仍可启动，接口返回友好提示；本地无 DATABASE_URL 时回退 MySQL
try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    PG_AVAILABLE = True
    PG_IMPORT_ERROR = None
except Exception as _pg_err:  # noqa
    PG_AVAILABLE = False
    PG_IMPORT_ERROR = str(_pg_err)

# ---- MySQL 驱动（仅本地回退模式使用）----
import pymysql

# ---- 真实 AI 识图（torch + torchvision + Pillow）----
# 未安装这些库时 AI_AVAILABLE=False，识图接口将拒绝猜测并提示用户手动输入
try:
    import torch
    from torchvision import models, transforms
    from PIL import Image
    AI_AVAILABLE = True
    AI_IMPORT_ERROR = None
except Exception as _e:  # noqa
    AI_AVAILABLE = False
    AI_IMPORT_ERROR = str(_e)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.normpath(os.path.join(BASE_DIR, '..', 'ui'))

# 前后端同源部署：Flask 直接托管 ui 目录（/ 与 /admin 由下方路由返回）
app = Flask(__name__, static_folder=UI_DIR, static_url_path='')

# 加载 jieba 自定义词典（避免物品名被切碎）
for _word in ['充电宝', '移动电源', '打火机', '防晒喷雾', '液体化妆品',
              '笔记本电脑', '折叠伞', '保温杯', '烟花鞭炮', '管制器具',
              '放射性物品', '易燃易爆物品', '超过160Wh锂电池', '旅行护肤品套装',
              '小型折叠自行车', '高尔夫球杆', '钓鱼用具', '婴儿奶粉', '婴儿推车',
              'CPAP呼吸机', '数码相机', '固体食品', '消毒液', '指甲油', '干冰',
              '手提包', '太阳镜', '文具盒', '卷笔刀', '密码锁', '储蓄罐',
              '电风扇', '听诊器', '望远镜', '创可贴', '注射器', '领带', '风衣']:
    jieba.add_word(_word)

def get_database_url():
    """
    云端模式只读一个环境变量 DATABASE_URL。
    Render 提供的可能是 postgres:// 前缀，这里统一规范化为 postgresql://。
    """
    url = (os.environ.get('DATABASE_URL') or '').strip()
    if not url:
        return None
    if url.startswith('postgres://'):
        url = 'postgresql://' + url[len('postgres://'):]
    if not url.startswith('postgresql://'):
        raise RuntimeError('DATABASE_URL 必须是 PostgreSQL 连接串（postgres:// 或 postgresql://）')
    return url


def get_mysql_config():
    """本地回退模式：全部来自 DB_* 环境变量，仓库内不保存任何密码。"""
    return {
        'host': os.environ.get('DB_HOST') or '127.0.0.1',
        'port': int(os.environ.get('DB_PORT') or 3306),
        'user': os.environ.get('DB_USER') or 'root',
        'password': os.environ.get('DB_PASSWORD') or '',
        'database': os.environ.get('DB_NAME') or 'travel_baggage',
        'charset': 'utf8mb4',
        'connect_timeout': int(os.environ.get('DB_CONNECT_TIMEOUT') or 10),
    }


def use_postgres():
    return bool((os.environ.get('DATABASE_URL') or '').strip())


def get_conn():
    """有 DATABASE_URL 走 PostgreSQL；否则回退本地 MySQL（本地开发用）。"""
    if use_postgres():
        if not PG_AVAILABLE:
            raise RuntimeError(f'PostgreSQL 驱动不可用：{PG_IMPORT_ERROR}')
        return psycopg2.connect(
            get_database_url(),
            connect_timeout=int(os.environ.get('DB_CONNECT_TIMEOUT') or 10),
        )
    return pymysql.connect(**get_mysql_config())


def dict_cursor(conn):
    """两种数据库统一返回字典游标，接口字段名保持一致。"""
    if use_postgres():
        return conn.cursor(cursor_factory=RealDictCursor)
    return conn.cursor(pymysql.cursors.DictCursor)


# ============================================================
# 云端启动初始化：自动建表 + 空库灌种子（仅 PostgreSQL 模式）
# ============================================================
try:
    from .seed_data import AIRLINE_WEIGHT_SEED, ITEM_KB_SEED
except ImportError:  # 以脚本方式直接运行 app.py 时
    from seed_data import AIRLINE_WEIGHT_SEED, ITEM_KB_SEED

PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS airline_weight (
    id SERIAL PRIMARY KEY,
    airline_name VARCHAR(255) NOT NULL,
    cabin VARCHAR(255) NOT NULL,
    weight_limit DOUBLE PRECISION,
    CONSTRAINT airline_weight_airline_cabin_key UNIQUE (airline_name, cabin)
);
CREATE TABLE IF NOT EXISTS item_kb (
    id SERIAL PRIMARY KEY,
    item_name VARCHAR(255) NOT NULL,
    keywords VARCHAR(255),
    carry_type VARCHAR(255),
    limit_desc VARCHAR(255),
    detail_explain TEXT,
    note VARCHAR(255),
    CONSTRAINT item_kb_item_name_key UNIQUE (item_name)
);
"""


def init_db():
    """启动时建表并在数据为空时灌种子；任何异常只告警，不影响应用启动。"""
    if not use_postgres():
        app.logger.warning('未配置 DATABASE_URL：跳过 PostgreSQL 初始化，走本地 MySQL 回退')
        return
    if not PG_AVAILABLE:
        app.logger.warning('psycopg2 不可用（%s）：跳过数据库初始化', PG_IMPORT_ERROR)
        return
    try:
        url = get_database_url()
        conn = psycopg2.connect(url, connect_timeout=int(os.environ.get('DB_CONNECT_TIMEOUT') or 10))
        try:
            with conn.cursor() as cur:
                cur.execute(PG_SCHEMA)
                cur.execute('SELECT COUNT(*) FROM airline_weight')
                if cur.fetchone()[0] == 0:
                    cur.executemany(
                        'INSERT INTO airline_weight (airline_name, cabin, weight_limit) '
                        'VALUES (%s, %s, %s) ON CONFLICT (airline_name, cabin) DO NOTHING',
                        AIRLINE_WEIGHT_SEED,
                    )
                cur.execute('SELECT COUNT(*) FROM item_kb')
                if cur.fetchone()[0] == 0:
                    cur.executemany(
                        'INSERT INTO item_kb (item_name, keywords, carry_type, limit_desc, '
                        'detail_explain, note) VALUES (%s, %s, %s, %s, %s, %s) '
                        'ON CONFLICT (item_name) DO NOTHING',
                        ITEM_KB_SEED,
                    )
            conn.commit()
            app.logger.info(
                'PostgreSQL 初始化完成：航司种子 %s 条，物品种子 %s 条',
                len(AIRLINE_WEIGHT_SEED), len(ITEM_KB_SEED),
            )
        finally:
            conn.close()
    except Exception as e:
        app.logger.warning('PostgreSQL 初始化失败（应用继续启动）：%s', e)


init_db()


# ------------------------------------------------------------
# 公共：查询物品知识库（item_query 与 image_recognize 复用）
# ------------------------------------------------------------
def query_item_kb(keyword):
    """根据关键词在 item_kb 中模糊匹配，返回单行字典或 None"""
    conn = get_conn()
    cursor = dict_cursor(conn)
    cursor.execute(
        "SELECT * FROM item_kb WHERE item_name LIKE %s OR keywords LIKE %s",
        (f'%{keyword}%', f'%{keyword}%')
    )
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row


# ------------------------------------------------------------
# 公共：智能关键词提取 + 查询
# 用户可能输入"香水能带吗"、"打火机可以带吗"等自然语言
# 用 jieba 分词后逐个词查询，找到就返回
# ------------------------------------------------------------
def query_item_name_only(keyword):
    """单字查询：只匹配 item_name 字段，避免误命中 keywords"""
    conn = get_conn()
    cursor = dict_cursor(conn)
    cursor.execute(
        "SELECT * FROM item_kb WHERE item_name LIKE %s",
        (f'%{keyword}%',)
    )
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row


def smart_query_item_kb(raw_text):
    """
    对用户输入做分词处理，逐个词查知识库。
    多字词：item_name + keywords 都查
    单字词：只查 item_name，避免误匹配
    """
    if not raw_text:
        return None, None

    # 1) 整句直接查
    row = query_item_kb(raw_text)
    if row:
        return row, raw_text

    # 2) jieba 分词
    words = [w.strip() for w in jieba.cut(raw_text) if w.strip()]

    stop_words = {'可以', '能不能', '能带', '可以带', '请问', '一下',
                  '是否', '携带', '带上', '飞机', '上飞机', '能上',
                  '可以上', '这个', '那个', '一个', '什么', '怎么',
                  '我', '想', '要', '的', '把', '个', '吗', '呢', '啊',
                  '带', '上', '过', '去', '来', '是', '有', '没', '不'}
    words = [w for w in words if w not in stop_words]

    # 3) 按长度从长到短排序（长词优先匹配）
    words.sort(key=len, reverse=True)

    # 4) 逐个词查
    for w in words:
        if len(w) >= 2:
            row = query_item_kb(w)
        else:
            row = query_item_name_only(w)
        if row:
            return row, w

    return None, None
# ------------------------------------------------------------
# 公共：根据 carry_type 解析是否可携带
# ------------------------------------------------------------
def parse_carry_type(carry_type):
    """
    根据 carry_type 字段文本判断携带规则。
    注意：'不可托运' 不等于违禁（只是不能托运，仍可随身），
          所以禁止类只匹配强否定词 '禁止/禁带/严禁'。
    """
    if not carry_type:
        return {'can_carry': False, 'is_forbidden': False, 'level': 'unknown'}
    ct = str(carry_type)
    # 禁止类：强禁止词
    if any(k in ct for k in ('禁止', '禁带', '严禁')):
        return {'can_carry': False, 'is_forbidden': True, 'level': 'forbidden'}
    # 限制携带
    if '限制' in ct:
        return {'can_carry': True, 'is_forbidden': False, 'level': 'limited'}
    # 允许类：出现 '可' 携带 / 随身 / 托运 / 允许 / 可以
    if any(k in ct for k in ('可携带', '可随身', '可托运', '随身', '托运', '允许', '可以')):
        return {'can_carry': True, 'is_forbidden': False, 'level': 'allowed'}
    return {'can_carry': False, 'is_forbidden': False, 'level': 'unknown'}


# ============================================================
# 前端页面（单进程托管，同源访问）
# ============================================================
@app.route('/')
def index():
    return send_from_directory(app.static_folder, 'all.html')


@app.route('/admin')
def admin_page():
    return send_from_directory(app.static_folder, '后台.html')


@app.route('/healthz')
def healthz():
    """Render 健康检查：只表示进程存活，不探测数据库。"""
    return jsonify({'ok': True})


# ============================================================
# 测试接口
# ============================================================
@app.route('/api/test', methods=['GET'])
def test():
    try:
        conn = get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM item_kb")
        count = cursor.fetchone()[0]
        cursor.close()
        conn.close()
        return jsonify({'success': True, 'msg': f'数据库连接成功，item_kb 有 {count} 条数据'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


# ============================================================
# 功能1：行李重量判断
# ============================================================
@app.route('/api/weight/query', methods=['GET'])
def weight_query():
    airline = request.args.get('airline', '').strip()
    cabin = request.args.get('cabin', '').strip()

    if not airline or not cabin:
        return jsonify({'found': False, 'msg': '请选择航司和舱位'})

    try:
        conn = get_conn()
        cursor = dict_cursor(conn)
        cursor.execute(
            "SELECT * FROM airline_weight WHERE airline_name=%s AND cabin=%s",
            (airline, cabin)
        )
        row = cursor.fetchone()
        cursor.close()
        conn.close()

        if row:
            return jsonify({
                'found': True,
                'airline': row['airline_name'],
                'cabin': row['cabin'],
                'weight_limit': row['weight_limit']
            })
        else:
            return jsonify({'found': False, 'msg': '暂时无法获取该航司/舱位的重量信息'})
    except Exception as e:
        return jsonify({'found': False, 'msg': '暂时无法获取航司重量信息', 'error': str(e)})


# ============================================================
# 功能2：物品携带问答（带 jieba 分词）
# ============================================================
@app.route('/api/item/query', methods=['GET'])
def item_query():
    raw = request.args.get('keyword', '').strip()

    if not raw:
        return jsonify({'found': False, 'answer': '请输入物品名称后再查询。'})

    try:
        row, matched_word = smart_query_item_kb(raw)
        if row:
            return jsonify({
                'found': True,
                'item_name': row['item_name'],
                'carry_type': row['carry_type'],
                'limit_desc': row['limit_desc'],
                'detail_explain': row['detail_explain'],
                'note': row['note'],
                'matched_keyword': matched_word
            })
        else:
            return jsonify({
                'found': False,
                'answer': '暂无此方面的信息，建议您查询航司官网或咨询机场安检。'
            })
    except Exception as e:
        return jsonify({'found': False, 'answer': '查询服务暂时不可用，请稍后重试。', 'error': str(e)})


# ============================================================
# 功能3：AI识图 —— 判断物品是否能带上飞机
# ============================================================
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'bmp', 'webp'}

MIN_CONFIDENCE = 0.05

FILENAME_KEYWORD_MAP = {
    '充电宝': '充电宝', 'powerbank': '充电宝', 'power bank': '充电宝',
    '喷雾': '防晒喷雾', 'spray': '防晒喷雾', '防晒': '防晒喷雾',
    '伞': '折叠伞', 'umbrella': '折叠伞',
    '电脑': '笔记本电脑', 'laptop': '笔记本电脑', '笔记本': '笔记本电脑',
    '水杯': '水杯', 'cup': '水杯', 'bottle': '水杯', '水壶': '水杯',
    '打火机': '打火机', 'lighter': '打火机',
    '牙膏': '牙膏', 'toothpaste': '牙膏',
    '香水': '香水', 'perfume': '香水',
}

IMAGENET_TO_CN = {
    'lighter': '打火机',
    'matchstick': '打火机',
    'perfume': '香水',
    'umbrella': '折叠伞',
    'parasol, sunshade': '折叠伞',
    'notebook, notebook computer': '笔记本电脑',
    'laptop, laptop computer': '笔记本电脑',
    'water bottle': '水杯',
    'cup': '水杯',
    'mug': '水杯',
    'coffee mug': '水杯',
}


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def recognize_item_from_filename(filename):
    """从上传图片的文件名中推断物品名称（最可靠，命中即用）"""
    name_lower = (filename or '').lower()
    for keyword, item in FILENAME_KEYWORD_MAP.items():
        if keyword.lower() in name_lower:
            return item
    return None


_AI_MODEL = None
_AI_PREPROCESS = None
_AI_LABELS = None
_AI_MODEL_ERROR = None


def _load_ai_model():
    """首次调用时加载 ResNet50，之后复用。返回 True/False。"""
    global _AI_MODEL, _AI_PREPROCESS, _AI_LABELS, _AI_MODEL_ERROR
    if not AI_AVAILABLE:
        return False
    if _AI_MODEL is not None:
        return True
    try:
        weights = models.ResNet50_Weights.DEFAULT
        _AI_MODEL = models.resnet50(weights=weights).eval()
        _AI_LABELS = weights.meta['categories']
        _AI_PREPROCESS = transforms.Compose([
            transforms.Resize(256),
            transforms.CenterCrop(224),
            transforms.ToTensor(),
            transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                 std=[0.229, 0.224, 0.225]),
        ])
        return True
    except Exception as e:
        _AI_MODEL_ERROR = str(e)
        return False


def _map_imagenet_label(en_label):
    """ImageNet 英文标签 -> 中文物品名（无映射返回 None）。"""
    en = en_label.strip().lower()
    if en in IMAGENET_TO_CN:
        return IMAGENET_TO_CN[en]
    first = en.split(',')[0].strip()
    if first in IMAGENET_TO_CN:
        return IMAGENET_TO_CN[first]
    for key, cn in IMAGENET_TO_CN.items():
        if cn and (key in en or first in key):
            return cn
    return None


def recognize_item_by_ai(file_storage):
    """用 ResNet50 真实识别图片。"""
    if not _load_ai_model():
        raise RuntimeError(f'AI 模型不可用: {_AI_MODEL_ERROR or AI_IMPORT_ERROR}')

    raw = file_storage.read()
    if not raw:
        raise RuntimeError('图片内容为空')

    img = Image.open(io.BytesIO(raw)).convert('RGB')
    x = _AI_PREPROCESS(img).unsqueeze(0)
    with torch.no_grad():
        out = _AI_MODEL(x)
        probs = torch.nn.functional.softmax(out, dim=1)[0]
        top = torch.topk(probs, 5)

    top5 = []
    for i in range(5):
        idx = top.indices[i].item()
        prob = top.values[i].item()
        en = _AI_LABELS[idx]
        cn = _map_imagenet_label(en)
        top5.append({'en': en, 'cn': cn, 'prob': prob})
        if cn and prob >= MIN_CONFIDENCE:
            return cn, en, prob, top5
    return None, top5[0]['en'], top5[0]['prob'], top5


def recognize_item_from_image(file_storage):
    """AI 识图主入口。"""
    extra = {}

    item = recognize_item_from_filename(file_storage.filename or '')
    if item:
        return item, 'filename', extra

    try:
        cn, en, prob, top5 = recognize_item_by_ai(file_storage)
        extra['top5'] = top5
        extra['confidence'] = round(prob, 4)
        if cn:
            return cn, 'ai', extra
        extra['raw_label'] = en
    except Exception as e:
        extra['ai_error'] = str(e)

    return None, 'unrecognized', extra


@app.route('/api/image/recognize', methods=['POST'])
def image_recognize():
    file = request.files.get('image') or request.files.get('file')
    if file is None:
        return jsonify({'success': False, 'msg': '请上传图片文件（字段名：image 或 file）'})
    if file.filename == '':
        return jsonify({'success': False, 'msg': '未选择文件'})
    if not allowed_file(file.filename):
        return jsonify({'success': False, 'msg': '不支持的图片格式，仅支持 png/jpg/jpeg/gif/bmp/webp'})

    recognized_item, recognize_source, extra = recognize_item_from_image(file)

    if recognized_item is None:
        ai_err = extra.get('ai_error') or AI_IMPORT_ERROR
        raw = extra.get('raw_label')
        hint = '无法识别该图片中的物品，请在"物品携带问答"中手动输入物品名称查询。'
        if not AI_AVAILABLE:
            hint += '（AI 识图功能暂未启用）'
        elif ai_err:
            hint += f'（AI 异常：{ai_err}）'
        elif raw:
            hint += f'（AI 识别为 {raw}，但不在已收录物品范围内）'
        return jsonify({
            'success': False,
            'recognized_item': None,
            'recognize_source': recognize_source,
            'confidence': extra.get('confidence'),
            'ai_available': AI_AVAILABLE,
            'raw_label': raw,
            'item_name': None,
            'carry_type': None,
            'can_carry': False,
            'is_forbidden': False,
            'carry_level': 'unknown',
            'msg': hint
        })

    try:
        row = query_item_kb(recognized_item)

        if row:
            carry_info = parse_carry_type(row.get('carry_type', ''))
            confidence = extra.get('confidence')
            msg = f'识别为「{row["item_name"]}」（来源：{recognize_source}'
            if confidence is not None:
                msg += f'，置信度 {confidence*100:.1f}%'
            msg += f'），携带类型：{row["carry_type"]}。{row.get("limit_desc", "") or ""}'
            return jsonify({
                'success': True,
                'recognized_item': recognized_item,
                'recognize_source': recognize_source,
                'confidence': confidence,
                'ai_available': AI_AVAILABLE,
                'item_name': row['item_name'],
                'carry_type': row['carry_type'],
                'limit_desc': row['limit_desc'],
                'detail_explain': row['detail_explain'],
                'note': row['note'],
                'can_carry': carry_info['can_carry'],
                'is_forbidden': carry_info['is_forbidden'],
                'carry_level': carry_info['level'],
                'msg': msg
            })
        else:
            raw = extra.get('raw_label')
            return jsonify({
                'success': True,
                'recognized_item': recognized_item,
                'recognize_source': recognize_source,
                'confidence': extra.get('confidence'),
                'ai_available': AI_AVAILABLE,
                'raw_label': raw,
                'item_name': recognized_item,
                'carry_type': '未知',
                'limit_desc': '',
                'detail_explain': '',
                'note': '',
                'can_carry': False,
                'is_forbidden': False,
                'carry_level': 'unknown',
                'msg': f'识别为「{recognized_item}」'
                      + (f'（AI 原始标签：{raw}）' if raw else '')
                      + '，但知识库中暂无该物品的携带规则，建议查询航司官网或咨询机场安检。'
            })
    except Exception as e:
        return jsonify({'success': False, 'msg': '识图服务暂时不可用', 'error': str(e)})


# ============================================================
# 后台管理接口：航司重量标准
# ============================================================

@app.route('/api/weight/list', methods=['GET'])
def weight_list():
    try:
        conn = get_conn()
        cursor = dict_cursor(conn)
        cursor.execute("SELECT * FROM airline_weight ORDER BY id")
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        return jsonify({'success': True, 'data': rows})
    except Exception as e:
        return jsonify({'success': False, 'msg': str(e)})


@app.route('/api/weight/save', methods=['POST'])
def weight_save():
    data = request.get_json(silent=True) or {}
    airline = (data.get('airline') or '').strip()
    cabin = (data.get('cabin') or '').strip()
    limit = data.get('limit')

    if not airline or not cabin or limit is None:
        return jsonify({'success': False, 'msg': '参数不完整'})

    try:
        limit = float(limit)
    except Exception:
        return jsonify({'success': False, 'msg': '重量必须是数字'})

    try:
        conn = get_conn()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id FROM airline_weight WHERE airline_name=%s AND cabin=%s",
            (airline, cabin)
        )
        row = cursor.fetchone()
        if row:
            cursor.execute(
                "UPDATE airline_weight SET weight_limit=%s WHERE id=%s",
                (limit, row[0])
            )
            action = '更新'
        else:
            cursor.execute(
                "INSERT INTO airline_weight (airline_name, cabin, weight_limit) VALUES (%s,%s,%s)",
                (airline, cabin, limit)
            )
            action = '新增'
        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({'success': True, 'msg': f'{action}成功'})
    except Exception as e:
        return jsonify({'success': False, 'msg': str(e)})


@app.route('/api/weight/delete', methods=['POST'])
def weight_delete():
    data = request.get_json(silent=True) or {}
    airline = (data.get('airline') or '').strip()
    cabin = (data.get('cabin') or '').strip()

    if not airline or not cabin:
        return jsonify({'success': False, 'msg': '参数不完整'})

    try:
        conn = get_conn()
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM airline_weight WHERE airline_name=%s AND cabin=%s",
            (airline, cabin)
        )
        conn.commit()
        affected = cursor.rowcount
        cursor.close()
        conn.close()
        if affected > 0:
            return jsonify({'success': True, 'msg': '删除成功'})
        else:
            return jsonify({'success': False, 'msg': '未找到该记录'})
    except Exception as e:
        return jsonify({'success': False, 'msg': str(e)})


# ============================================================
# 后台管理接口：物品知识库
# ============================================================

@app.route('/api/item/list', methods=['GET'])
def item_list():
    try:
        conn = get_conn()
        cursor = dict_cursor(conn)
        cursor.execute("SELECT * FROM item_kb ORDER BY id")
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        return jsonify({'success': True, 'data': rows})
    except Exception as e:
        return jsonify({'success': False, 'msg': str(e)})


@app.route('/api/item/add', methods=['POST'])
def item_add():
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'success': False, 'msg': '物品名称不能为空'})

    try:
        conn = get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM item_kb WHERE item_name=%s", (name,))
        if cursor.fetchone():
            cursor.close()
            conn.close()
            return jsonify({'success': False, 'msg': '该物品已存在'})

        cursor.execute(
            "INSERT INTO item_kb (item_name, keywords, carry_type, limit_desc, detail_explain, note) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (
                name,
                data.get('keywords', ''),
                data.get('carryType', ''),
                data.get('limitDesc', ''),
                data.get('detail', ''),
                data.get('note', '')
            )
        )
        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({'success': True, 'msg': '新增成功'})
    except Exception as e:
        return jsonify({'success': False, 'msg': str(e)})


@app.route('/api/item/delete', methods=['POST'])
def item_delete():
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'success': False, 'msg': '参数不完整'})

    try:
        conn = get_conn()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM item_kb WHERE item_name=%s", (name,))
        conn.commit()
        affected = cursor.rowcount
        cursor.close()
        conn.close()
        if affected > 0:
            return jsonify({'success': True, 'msg': '删除成功'})
        else:
            return jsonify({'success': False, 'msg': '未找到该物品'})
    except Exception as e:
        return jsonify({'success': False, 'msg': str(e)})


@app.route('/api/item/update', methods=['POST'])
def item_update():
    data = request.get_json(silent=True) or {}
    old_name = (data.get('oldName') or '').strip()
    new_name = (data.get('name') or '').strip()
    if not old_name or not new_name:
        return jsonify({'success': False, 'msg': '物品名称不能为空'})

    try:
        conn = get_conn()
        cursor = conn.cursor()
        # 若改了名称，检查新名称是否已被占用
        if new_name != old_name:
            cursor.execute("SELECT id FROM item_kb WHERE item_name=%s", (new_name,))
            if cursor.fetchone():
                cursor.close()
                conn.close()
                return jsonify({'success': False, 'msg': '该物品名称已存在'})

        cursor.execute(
            "UPDATE item_kb SET item_name=%s, keywords=%s, carry_type=%s, "
            "limit_desc=%s, detail_explain=%s, note=%s WHERE item_name=%s",
            (
                new_name,
                data.get('keywords', ''),
                data.get('carryType', ''),
                data.get('limitDesc', ''),
                data.get('detail', ''),
                data.get('note', ''),
                old_name
            )
        )
        conn.commit()
        affected = cursor.rowcount
        cursor.close()
        conn.close()
        if affected > 0:
            return jsonify({'success': True, 'msg': '修改成功'})
        else:
            return jsonify({'success': False, 'msg': '未找到该物品'})
    except Exception as e:
        return jsonify({'success': False, 'msg': str(e)})


if __name__ == '__main__':
    # 本地开发：python contral/app.py 后访问 http://127.0.0.1:5000
    port = int(os.environ.get('PORT') or 5000)
    debug = (os.environ.get('FLASK_DEBUG') or '').lower() in ('1', 'true', 'yes')
    app.run(debug=debug, host='0.0.0.0', port=port)