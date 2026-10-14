from app.domain.schemas import CreateDatasetRequest
from app.repositories.audit_repository import add_log
from app.repositories.resource_repository import create_dataset


def create_new_dataset(request: CreateDatasetRequest):
    """
    创建模拟数据集，并记录资源创建日志。
    """

    dataset_data = {
        "name": request.name,
        "source_type": request.source_type,
        "description": request.description,
        "record_count": request.record_count,
        "languages": request.languages,
    }

    # 保存数据集
    dataset = create_dataset(dataset_data)

    # 记录资源变更日志
    add_log(
        task_id=None,
        event_type="dataset_created",
        request_data=dataset_data,
        response_data=dataset,
    )

    return dataset
