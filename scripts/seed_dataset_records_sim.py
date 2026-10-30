# -*- coding: utf-8 -*-
"""为现有每个数据集模拟补登 10 条样本记录，并登记新的完整原始版本（#13 口径）。

- dataset_records：每个数据集写入 10 条 source_record_id=NULL 的原件行，
  content_hash 与接入服务同口径（canonical JSON sha256），task_id 用
  tsk_sim_seed_<dataset_id:02d> 标记模拟批次。
- dataset_versions / dataset_version_items：调用 create_ingest_version 创建新快照，
  保留旧版本，并保证新版本的 sample_count、manifest_hash、items 与原件记录一致。

幂等：某数据集已存在 tsk_sim_seed_ 开头的记录时跳过，重复运行不会翻倍。
"""
from __future__ import annotations

import random
import sys

from sqlalchemy import func, select

sys.path.insert(0, r"D:\数据治理\Content_Safety_Backend")

from app.core.database import SessionLocal
from app.models.tables import Dataset, DatasetRecord, DatasetVersion, DatasetVersionItem
from app.services.database_ingest import record_hash
from app.services.dataset_version_service import create_ingest_version

SIM_TASK_PREFIX = "tsk_sim_seed_"
PER_DATASET = 10

# 每个数据集的模拟样本模板（字段贴合类别，均含 text/language 供样本展示接口使用）。
TEMPLATES = {
    1: {  # 跨文化交流语料（语料数据 / business，zh+en）
        "fields": ["text", "language", "topic", "region"],
        "rows": [
            {"text": "春节拜年时，晚辈应向长辈拱手作揖并说吉祥话，长辈则回赠红包。", "language": "zh", "topic": "节日习俗", "region": "中国大陆"},
            {"text": "商务饭局上，主人先举杯敬酒，客人回敬时杯沿应低于对方以示尊重。", "language": "zh", "topic": "商务礼仪", "region": "中国大陆"},
            {"text": "送礼物时避免送钟表或伞，因谐音不吉利，是常见的中式禁忌。", "language": "zh", "topic": "送礼文化", "region": "中国大陆"},
            {"text": "北方酒桌文化中，敬酒讲究'先干为敬'，宾客离席前需再次敬酒致谢。", "language": "zh", "topic": "餐桌文化", "region": "华北"},
            {"text": "广东地区的茶楼早茶讲究'一盅两件'，长辈会给晚辈斟茶以示关怀。", "language": "zh", "topic": "地域风俗", "region": "华南"},
            {"text": "In Japan, bowing depth and duration signal the level of respect in business greetings.", "language": "en", "topic": "Greetings", "region": "Japan"},
            {"text": "Small talk about weather is a common ice-breaker in British professional settings.", "language": "en", "topic": "Small Talk", "region": "UK"},
            {"text": "In the US, punctuality for meetings is highly valued and arriving early is preferred.", "language": "en", "topic": "Business Etiquette", "region": "USA"},
            {"text": "Tipping culture varies widely: 15-20% is expected in US restaurants but rare in Japan.", "language": "en", "topic": "Tipping", "region": "USA"},
            {"text": "During Ramadan, business lunches are often replaced by evening gatherings after sunset.", "language": "en", "topic": "Cultural Festivals", "region": "Middle East"},
        ],
    },
    2: {  # 行业风险标注数据集（风险数据 / industry，zh）
        "fields": ["text", "language", "risk_label", "risk_level", "industry"],
        "rows": [
            {"text": "【模拟样本】用户收到自称客服的来电，要求下载不明App并共享屏幕处理退款。", "language": "zh", "risk_label": "网络诈骗", "risk_level": "high", "industry": "电商"},
            {"text": "【模拟样本】群聊中有人发布'扫码入群领现金红包，稳赚不赔'的引流话术。", "language": "zh", "risk_label": "赌博引流", "risk_level": "high", "industry": "社交"},
            {"text": "【模拟样本】帖子使用辱骂性语言攻击特定地域人群，引发评论区对立。", "language": "zh", "risk_label": "网络暴力", "risk_level": "mid", "industry": "社区"},
            {"text": "【模拟样本】广告文案夸大保健品疗效，暗示可替代正规药物治疗。", "language": "zh", "risk_label": "虚假广告", "risk_level": "mid", "industry": "医疗"},
            {"text": "【模拟样本】短视频标题称'某地即将停水停电'，内容与官方通告不符。", "language": "zh", "risk_label": "谣言误导", "risk_level": "mid", "industry": "资讯"},
            {"text": "【模拟样本】平台出售账号的页面引导买家私下交易并索要身份证照片。", "language": "zh", "risk_label": "隐私泄露", "risk_level": "high", "industry": "游戏"},
            {"text": "【模拟样本】直播间主播反复催促观众'点击链接开通会员，不满意秒退'。", "language": "zh", "risk_label": "诱导消费", "risk_level": "low", "industry": "直播"},
            {"text": "【模拟样本】兼职信息声称'动动手指日入过千'，要求先缴纳培训费。", "language": "zh", "risk_label": "刷单返利", "risk_level": "high", "industry": "招聘"},
            {"text": "【模拟样本】网页冒充银行官方界面，提示输入网银账号与短信验证码。", "language": "zh", "risk_label": "假冒官方", "risk_level": "high", "industry": "金融"},
            {"text": "【模拟样本】评论区出现明显低俗擦边描述，配图内容含挑逗暗示。", "language": "zh", "risk_label": "低俗内容", "risk_level": "low", "industry": "社交"},
        ],
    },
    3: {  # 社交媒体中文语料库（互联网数据 / internet，zh）
        "fields": ["text", "language", "platform", "author", "publish_time", "hot_score"],
        "rows": [
            {"text": "今天通勤路上看到晚霞特别好看，随手拍了一张分享给大家。", "language": "zh", "platform": "微博", "author": "晚风拾光", "publish_time": "2026-10-21 18:42", "hot_score": 86},
            {"text": "新买的降噪耳机实测：地铁通勤降噪效果明显，续航也够用一整天。", "language": "zh", "platform": "小红书", "author": "数码小北", "publish_time": "2026-10-22 09:15", "hot_score": 72},
            {"text": "周末去爬了城郊的山，全程五小时，山顶风景确实值得。", "language": "zh", "platform": "抖音", "author": "山野行者", "publish_time": "2026-10-23 20:08", "hot_score": 64},
            {"text": "理性讨论：城市通勤到底选地铁还是自驾，各有各的账要算。", "language": "zh", "platform": "知乎", "author": "通勤观察员", "publish_time": "2026-10-24 12:30", "hot_score": 91},
            {"text": "今天食堂的招牌菜换了新做法，味道比之前更好，价格没变。", "language": "zh", "platform": "贴吧", "author": "干饭第一名", "publish_time": "2026-10-24 12:45", "hot_score": 35},
            {"text": "小区楼下新开了家便利店，深夜买东西方便多了，价格也公道。", "language": "zh", "platform": "微博", "author": "小区情报站", "publish_time": "2026-10-25 08:20", "hot_score": 48},
            {"text": "安利一部纪录片，镜头语言和配乐都很讲究，看完感触很深。", "language": "zh", "platform": "小红书", "author": "胶片收藏家", "publish_time": "2026-10-25 21:33", "hot_score": 57},
            {"text": "健身打卡第30天，体重没怎么变但体态肉眼可见地好了。", "language": "zh", "platform": "抖音", "author": "自律的阿杰", "publish_time": "2026-10-26 07:50", "hot_score": 41},
            {"text": "关于新规的疑问：电动车进电梯到底怎么界定，有没有官方说明。", "language": "zh", "platform": "知乎", "author": "规则爱好者", "publish_time": "2026-10-26 15:05", "hot_score": 79},
            {"text": "傍晚遛狗偶遇彩虹，抓拍到了，运气不错的一天。", "language": "zh", "platform": "微博", "author": "铲屎官日常", "publish_time": "2026-10-27 18:10", "hot_score": 28},
        ],
    },
    4: {  # 多模态内容安全样本集（多模态数据 / synthetic，zh+en，text/image/video）
        "fields": ["text", "language", "modality", "content_type", "risk_label"],
        "rows": [
            {"text": "【模拟样本】视频字幕出现引导用户点击外部链接领取高额返利的弹窗。", "language": "zh", "modality": "video", "content_type": "短视频", "risk_label": "诱导引流"},
            {"text": "【模拟样本】图片配文声称某保健品可根治慢性病，配图含虚假权威证书。", "language": "zh", "modality": "image", "content_type": "图文广告", "risk_label": "虚假宣传"},
            {"text": "正常内容：今日份治愈系风景照，蓝天白云和远处的雪山。", "language": "zh", "modality": "image", "content_type": "生活分享", "risk_label": "正常"},
            {"text": "【模拟样本】直播口播反复索要观众手机号，承诺免费邮寄试用装。", "language": "zh", "modality": "video", "content_type": "直播", "risk_label": "个人信息收集"},
            {"text": "正常内容：新书开箱视频，纸质装帧和印刷质量都很满意。", "language": "zh", "modality": "video", "content_type": "开箱测评", "risk_label": "正常"},
            {"text": "【模拟样本】评论区短链接配合诱导文案，疑似钓鱼外链。", "language": "zh", "modality": "text", "content_type": "评论", "risk_label": "钓鱼外链"},
            {"text": "【Simulated sample】A generated image overlay simulates a fake lottery jackpot screen.", "language": "en", "modality": "image", "content_type": "ad", "risk_label": "scam_display"},
            {"text": "【Simulated sample】ASR transcript contains repeated gambling jargon and bonus promises.", "language": "en", "modality": "video", "content_type": "short_video", "risk_label": "gambling_promotion"},
            {"text": "Normal content: product unboxing video, clear focus and decent lighting.", "language": "en", "modality": "video", "content_type": "unboxing", "risk_label": "normal"},
            {"text": "【模拟样本】OCR 文本含'加群领福利'字样与疑似违规群号。", "language": "zh", "modality": "image", "content_type": "截图", "risk_label": "违规引流"},
        ],
    },
}


def build_payload(dataset_id: int, index: int, rng: random.Random) -> dict:
    """按数据集模板取第 index 条样本（模板内顺序固定，避免随机漂移）。"""
    template = TEMPLATES[dataset_id]
    row = dict(template["rows"][index])
    row["sample_no"] = f"sim_{dataset_id:02d}_{index + 1:02d}"
    return row


def main() -> int:
    with SessionLocal() as db:
        datasets = db.scalars(select(Dataset).order_by(Dataset.id)).all()
        if not datasets:
            print("datasets 表为空，无需模拟。")
            return 0

        for dataset in datasets:
            dataset_id = dataset.id
            task_id = f"{SIM_TASK_PREFIX}{dataset_id:02d}"
            existing = db.scalar(
                select(func.count(DatasetRecord.id)).where(
                    DatasetRecord.dataset_id == dataset_id,
                    DatasetRecord.task_id.like(f"{SIM_TASK_PREFIX}%"),
                )
            )
            if existing:
                print(f"[skip] dataset_id={dataset_id} 已有 {existing} 条模拟记录，跳过。")
                continue
            template = TEMPLATES.get(dataset_id)
            if template is None:
                print(f"[warn] dataset_id={dataset_id} 无模拟模板，跳过。")
                continue

            rng = random.Random(20261028 + dataset_id)
            rows = [build_payload(dataset_id, i, rng) for i in range(PER_DATASET)]
            hashes = [record_hash(row) for row in rows]
            # 确认没有重复指纹（模拟数据不应触发重复去重）。
            assert len(set(hashes)) == len(hashes), "模拟样本出现重复内容哈希"

            db.add_all([
                DatasetRecord(
                    dataset_id=dataset_id,
                    task_id=task_id,
                    content_hash=content_hash,
                    source_record_id=None,
                    payload=row,
                )
                for row, content_hash in zip(rows, hashes)
            ])
            db.flush()
            # 改之前：补登样本会改写现有原始版本；改之后：创建含旧样本和新样本的新版本。
            version = create_ingest_version(db, dataset, task_id=task_id)
            print(
                f"[ok] dataset_id={dataset_id} {dataset.name}: 写入 {len(rows)} 条记录, "
                f"task_id={task_id}, 版本 {version.version_id} sample_count={version.sample_count}"
            )

        db.commit()

        # 提交后汇总核对
        print("\n=== 提交后核对 ===")
        problems = []
        for dataset in db.scalars(select(Dataset).order_by(Dataset.id)).all():
            record_count = db.scalar(
                select(func.count(DatasetRecord.id)).where(DatasetRecord.dataset_id == dataset.id)
            )
            raw_count = db.scalar(
                select(func.count(DatasetRecord.id)).where(
                    DatasetRecord.dataset_id == dataset.id,
                    DatasetRecord.source_record_id.is_(None),
                )
            )
            item_count = db.scalar(
                select(func.count(DatasetVersionItem.id)).join(
                    DatasetVersion, DatasetVersion.version_id == DatasetVersionItem.version_id
                ).where(DatasetVersion.dataset_id == dataset.id)
            )
            print(
                f"dataset_id={dataset.id} {dataset.name}: records={record_count} "
                f"raw={raw_count} version_items={item_count}"
            )
            if item_count != raw_count:
                problems.append(f"dataset_id={dataset.id} version_items({item_count}) != raw_records({raw_count})")
        if problems:
            print("\n[FAIL] " + "; ".join(problems))
            return 1
        print("[PASS] 每个数据集的版本条目数与原始记录数一致。")
        return 0


if __name__ == "__main__":
    sys.exit(main())
