import datetime

import peewee
from peewee import InterfaceError, OperationalError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from api.db.db_models import DB
from common.misc_utils import get_uuid
from common.time_utils import current_timestamp, datetime_format


def retry_db_operation(func):
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=5),
        retry=retry_if_exception_type((InterfaceError, OperationalError)),
        before_sleep=lambda retry_state: print(f"RETRY {retry_state.attempt_number} TIMES"),
        reraise=True,
    )
    def wrapper(*args, **kwargs):
        return func(*args, **kwargs)

    return wrapper


class CommonService:
    model = None

    @classmethod
    @DB.connection_context()
    def query(cls, cols=None, reverse=None, order_by=None, **kwargs):

        return cls.model.query(cols=cols, reverse=reverse, order_by=order_by, **kwargs)

    @classmethod
    @DB.connection_context()
    def get_all(cls, cols=None, reverse=None, order_by=None, **kwargs):

        if cols:
            query_records = cls.model.select(*cols)
        else:
            query_records = cls.model.select()
        if reverse is not None:
            if not order_by or not hasattr(cls.model, order_by):
                order_by = "create_time"
            if reverse is True:
                query_records = query_records.order_by(cls.model.getter_by(order_by).desc())
            elif reverse is False:
                query_records = query_records.order_by(cls.model.getter_by(order_by).asc())
        return query_records

    @classmethod
    @DB.connection_context()
    def get(cls, **kwargs):
        return cls.model.get(**kwargs)

    @classmethod
    @DB.connection_context()
    def get_or_none(cls, **kwargs):
        try:
            return cls.model.get(**kwargs)
        except peewee.DoesNotExist:
            return None

    @classmethod
    @DB.connection_context()
    def save(cls, **kwargs):
        sample_obj = cls.model(**kwargs).save(force_insert=True)
        return sample_obj

    @classmethod
    @DB.connection_context()
    def insert(cls, **kwargs):
        if "id" not in kwargs:
            kwargs["id"] = get_uuid()
        timestamp = current_timestamp()
        cur_datetime = datetime_format(datetime.now())
        kwargs["create_time"] = timestamp
        kwargs["create_date"] = cur_datetime
        kwargs["update_time"] = timestamp
        kwargs["update_date"] = cur_datetime
        sample_obj = cls.model(**kwargs).save(force_insert=True)
        return sample_obj

    @classmethod
    @DB.connection_context()
    def insert_many(cls, data_list, batch_size=100):

        current_ts = current_timestamp()
        current_datetime = datetime_format(datetime.now())
        with DB.atomic():
            for d in data_list:
                d["create_time"] = current_ts
                d["create_date"] = current_datetime
                d["update_time"] = current_ts
                d["update_date"] = current_datetime

            for i in range(0, len(data_list), batch_size):
                cls.model.insert_many(data_list[i : i + batch_size]).execute()

    @classmethod
    @DB.connection_context()
    def update_many_by_id(cls, data_list):
        timestampt = current_timestamp()
        cur_datetime = datetime_format(datetime.now())

        for data in data_list:
            data["update_time"] = timestampt
            data["update_date"] = cur_datetime
        with DB.atomic():
            for data in data_list:
                cls.model.update(data).where(cls.model.id == data["id"]).execute()

    @classmethod
    @DB.connection_context()
    @retry_db_operation
    def update_by_id(cls, pid, data):

        data["update_time"] = current_timestamp()
        data["update_date"] = datetime_format(datetime.now())

        replace_update = getattr(DB, "replace_update_by_id", None)
        if replace_update is not None:
            num = replace_update(cls.model, pid, data)
            if num is not None:
                return num
        prepare_update = getattr(DB, "prepare_update_by_id", None)
        if prepare_update is not None:
            data = prepare_update(cls.model, pid, data)
        num = cls.model.update(data).where(cls.model.id == pid).execute()
        return num

    @classmethod
    @DB.connection_context()
    def get_by_id(cls, pid):
        try:
            obj = cls.model.get_or_none(cls.model.id == pid)
            if obj:
                return True, obj
        except Exception:
            pass
        return False, None

    @classmethod
    @DB.connection_context()
    def get_by_ids(cls, pids, cols=None):
        # Get multiple records by their IDs
        # Args:
        #     pids: List of record IDs
        #     cols: List of columns to select
        # Returns:
        #     Query of matching records
        if cols:
            objs = cls.model.select(*cols)
        else:
            objs = cls.model.select()
        return objs.where(cls.model.id.in_(pids))

    @classmethod
    @DB.connection_context()
    def delete_by_id(cls, pid):
        # Delete a record by ID
        # Args:
        #     pid: Record ID
        # Returns:
        #     Number of records deleted
        return cls.model.delete().where(cls.model.id == pid).execute()

    @classmethod
    @DB.connection_context()
    def delete_by_ids(cls, pids):
        # Delete multiple records by their IDs
        # Args:
        #     pids: List of record IDs
        # Returns:
        #     Number of records deleted
        with DB.atomic():
            res = cls.model.delete().where(cls.model.id.in_(pids)).execute()
            return res

    @classmethod
    @DB.connection_context()
    def filter_delete(cls, filters):
        # Delete records matching given filters
        # Args:
        #     filters: List of filter conditions
        # Returns:
        #     Number of records deleted
        with DB.atomic():
            num = cls.model.delete().where(*filters).execute()
            return num

    @classmethod
    @DB.connection_context()
    def filter_update(cls, filters, update_data):
        # Update records matching given filters
        # Args:
        #     filters: List of filter conditions
        #     update_data: Updated field values
        # Returns:
        #     Number of records updated
        with DB.atomic():
            return cls.model.update(update_data).where(*filters).execute()

    @staticmethod
    def cut_list(tar_list, n):
        # Split a list into chunks of size n
        # Args:
        #     tar_list: List to split
        #     n: Chunk size
        # Returns:
        #     List of tuples containing chunks
        length = len(tar_list)
        arr = range(length)
        result = [tuple(tar_list[x : (x + n)]) for x in arr[::n]]
        return result

    @classmethod
    @DB.connection_context()
    def filter_scope_list(cls, in_key, in_filters_list, filters=None, cols=None):
        # Get records matching IN clause filters with optional column selection
        # Args:
        #     in_key: Field name for IN clause
        #     in_filters_list: List of values for IN clause
        #     filters: Additional filter conditions
        #     cols: List of columns to select
        # Returns:
        #     List of matching records
        in_filters_tuple_list = cls.cut_list(in_filters_list, 20)
        if not filters:
            filters = []
        res_list = []
        if cols:
            for i in in_filters_tuple_list:
                query_records = cls.model.select(*cols).where(getattr(cls.model, in_key).in_(i), *filters)
                if query_records:
                    res_list.extend([query_record for query_record in query_records])
        else:
            for i in in_filters_tuple_list:
                query_records = cls.model.select().where(getattr(cls.model, in_key).in_(i), *filters)
                if query_records:
                    res_list.extend([query_record for query_record in query_records])
        return res_list
