"""接口出入参模型：列表分页、动作结果与各模块的明细结构。"""
from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class PageResult(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int = 1
    size: int = 20


class ActionResult(BaseModel):
    ok: bool
    message: str
    entry: dict[str, Any] | None = None


class EntryPayload(BaseModel):
    """登记或修改一条业务记录时提交的字段集合。"""

    values: dict[str, Any] = Field(default_factory=dict)
    remark: str | None = None


class ImportSessionCreate(BaseModel):
    """创建断点续传上传会话。"""

    file_name: str = Field(..., description="来件原始文件名")


class ImportSessionRows(BaseModel):
    """断点续传：按行偏移量追加一批原始行（第一行为表头）。"""

    offset: int = Field(..., ge=0, description="本批首行在整份文件中的行偏移量")
    rows: list[str] = Field(default_factory=list, description="本批包含表头在内的原始文本行")


class ImportCommitPayload(BaseModel):
    """整文件一次性预览/提交：前端也可绕过会话直接送全文。"""

    file_name: str
    content: str
    fingerprint: str | None = Field(default=None, description="前端预算的 SHA-256，服务端会复核")


class QuarantineResolvePayload(BaseModel):
    """缺孔号隔离行现场补号后迁入台账。"""

    borehole_code: str = Field(..., alias="钻孔编号")

    model_config = {"populate_by_name": True}


class TodoStatusPayload(BaseModel):
    """偏离待办关闭/重开。"""

    status: str = Field(..., description="待处理 / 已处理")



class BoreholeEntry(BaseModel):
    """钻孔明细结构。"""

    field_0: str | None = None  # 钻孔编号
    field_1: str | None = None  # 勘探区
    field_2: str | None = None  # 孔口坐标
    field_3: str | None = None  # 设计孔深
    field_4: str | None = None  # 终孔深度
    field_5: str | None = None  # 开孔日期
    field_6: str | None = None  # 终孔日期
    field_7: str | None = None  # 钻孔状态

class CoreEntry(BaseModel):
    """岩心样本明细结构。"""

    field_0: str | None = None  # 岩心编号
    field_1: str | None = None  # 所属钻孔
    field_2: str | None = None  # 取样深度起
    field_3: str | None = None  # 取样深度止
    field_4: str | None = None  # 岩性描述
    field_5: str | None = None  # 采取率
    field_6: str | None = None  # 存放位置
    field_7: str | None = None  # 样本状态

class StratigraphyEntry(BaseModel):
    """地层单元明细结构。"""

    field_0: str | None = None  # 单元编号
    field_1: str | None = None  # 钻孔编号
    field_2: str | None = None  # 地层名称
    field_3: str | None = None  # 顶界深度
    field_4: str | None = None  # 底界深度
    field_5: str | None = None  # 厚度
    field_6: str | None = None  # 岩性组合
    field_7: str | None = None  # 划分依据

class GeophysicsEntry(BaseModel):
    """物探测线明细结构。"""

    field_0: str | None = None  # 测线编号
    field_1: str | None = None  # 勘探区
    field_2: str | None = None  # 物探方法
    field_3: str | None = None  # 测线长度
    field_4: str | None = None  # 点距
    field_5: str | None = None  # 施测日期
    field_6: str | None = None  # 数据质量
    field_7: str | None = None  # 测线状态

class GeochemEntry(BaseModel):
    """化探样品明细结构。"""

    field_0: str | None = None  # 样品编号
    field_1: str | None = None  # 样品类型
    field_2: str | None = None  # 采样点位
    field_3: str | None = None  # 分析元素
    field_4: str | None = None  # 检测方法
    field_5: str | None = None  # 检出限
    field_6: str | None = None  # 分析日期
    field_7: str | None = None  # 样品状态

class AssayEntry(BaseModel):
    """化验结果明细结构。"""

    field_0: str | None = None  # 化验编号
    field_1: str | None = None  # 样品编号
    field_2: str | None = None  # 元素名称
    field_3: str | None = None  # 化验值
    field_4: str | None = None  # 单位
    field_5: str | None = None  # 化验方法
    field_6: str | None = None  # 化验日期
    field_7: str | None = None  # 结果状态

class MappingEntry(BaseModel):
    """填图单元明细结构。"""

    field_0: str | None = None  # 图幅编号
    field_1: str | None = None  # 图幅名称
    field_2: str | None = None  # 比例尺
    field_3: str | None = None  # 填图面积
    field_4: str | None = None  # 填图人员
    field_5: str | None = None  # 野外日期
    field_6: str | None = None  # 室内整理
    field_7: str | None = None  # 填图状态

class SurveyPointEntry(BaseModel):
    """控制点明细结构。"""

    field_0: str | None = None  # 点号
    field_1: str | None = None  # 点类型
    field_2: str | None = None  # 坐标X
    field_3: str | None = None  # 坐标Y
    field_4: str | None = None  # 高程
    field_5: str | None = None  # 精度等级
    field_6: str | None = None  # 观测日期
    field_7: str | None = None  # 点位状态

class DrillingLogEntry(BaseModel):
    """钻探记录明细结构。"""

    field_0: str | None = None  # 日志编号
    field_1: str | None = None  # 钻孔编号
    field_2: str | None = None  # 钻进深度
    field_3: str | None = None  # 回次进尺
    field_4: str | None = None  # 岩层描述
    field_5: str | None = None  # 水位深度
    field_6: str | None = None  # 钻探人员
    field_7: str | None = None  # 日志状态

class ReserveEntry(BaseModel):
    """矿体块段明细结构。"""

    field_0: str | None = None  # 块段编号
    field_1: str | None = None  # 矿体名称
    field_2: str | None = None  # 面积
    field_3: str | None = None  # 厚度
    field_4: str | None = None  # 品位
    field_5: str | None = None  # 矿石体重
    field_6: str | None = None  # 资源类别
    field_7: str | None = None  # 块段状态

class SampleRegistryEntry(BaseModel):
    """送检样品明细结构。"""

    field_0: str | None = None  # 送检编号
    field_1: str | None = None  # 样品名称
    field_2: str | None = None  # 采样位置
    field_3: str | None = None  # 检测项目
    field_4: str | None = None  # 送检单位
    field_5: str | None = None  # 收样日期
    field_6: str | None = None  # 检测周期
    field_7: str | None = None  # 送检状态

class EquipmentEntry(BaseModel):
    """勘探仪器明细结构。"""

    field_0: str | None = None  # 仪器编号
    field_1: str | None = None  # 仪器名称
    field_2: str | None = None  # 型号规格
    field_3: str | None = None  # 精度指标
    field_4: str | None = None  # 检定日期
    field_5: str | None = None  # 有效期至
    field_6: str | None = None  # 使用人员
    field_7: str | None = None  # 仪器状态

class HydroEntry(BaseModel):
    """水文观测点明细结构。"""

    field_0: str | None = None  # 观测编号
    field_1: str | None = None  # 观测类型
    field_2: str | None = None  # 所在钻孔
    field_3: str | None = None  # 静止水位
    field_4: str | None = None  # 降深
    field_5: str | None = None  # 出水量
    field_6: str | None = None  # 观测日期
    field_7: str | None = None  # 观测状态

class SectionEntry(BaseModel):
    """实测剖面明细结构。"""

    field_0: str | None = None  # 剖面编号
    field_1: str | None = None  # 剖面名称
    field_2: str | None = None  # 剖面长度
    field_3: str | None = None  # 起点坐标
    field_4: str | None = None  # 终点坐标
    field_5: str | None = None  # 编录日期
    field_6: str | None = None  # 编录人员
    field_7: str | None = None  # 剖面状态

class GeologicalReportEntry(BaseModel):
    """勘探报告明细结构。"""

    field_0: str | None = None  # 报告编号
    field_1: str | None = None  # 勘探区
    field_2: str | None = None  # 报告类型
    field_3: str | None = None  # 编制人
    field_4: str | None = None  # 审核人
    field_5: str | None = None  # 提交日期
    field_6: str | None = None  # 审定结论
    field_7: str | None = None  # 报告状态

class RemoteEntry(BaseModel):
    """遥感数据明细结构。"""

    field_0: str | None = None  # 数据编号
    field_1: str | None = None  # 数据源
    field_2: str | None = None  # 分辨率
    field_3: str | None = None  # 覆盖面积
    field_4: str | None = None  # 获取日期
    field_5: str | None = None  # 解译内容
    field_6: str | None = None  # 解译人员
    field_7: str | None = None  # 数据状态

class MineralEntry(BaseModel):
    """矿化线索明细结构。"""

    field_0: str | None = None  # 线索编号
    field_1: str | None = None  # 勘探区
    field_2: str | None = None  # 矿种
    field_3: str | None = None  # 矿化类型
    field_4: str | None = None  # 发现方式
    field_5: str | None = None  # 踏勘日期
    field_6: str | None = None  # 评价结论
    field_7: str | None = None  # 线索状态

class EnvironmentalEntry(BaseModel):
    """环境调查点明细结构。"""

    field_0: str | None = None  # 调查编号
    field_1: str | None = None  # 调查区域
    field_2: str | None = None  # 灾害类型
    field_3: str | None = None  # 危害等级
    field_4: str | None = None  # 影响范围
    field_5: str | None = None  # 调查日期
    field_6: str | None = None  # 调查人员
    field_7: str | None = None  # 调查状态
