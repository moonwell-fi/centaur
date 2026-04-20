from gsuite import client


class _CreateRequest:
    def __init__(self, result: dict):
        self._result = result

    def execute(self) -> dict:
        return self._result


class _FakeFilesApi:
    def __init__(self):
        self.create_calls: list[dict] = []

    def create(self, **kwargs):
        self.create_calls.append(kwargs)
        if kwargs["body"].get("mimeType") == "application/vnd.google-apps.folder":
            return _CreateRequest(
                {
                    "id": "folder-123",
                    "name": kwargs["body"]["name"],
                    "webViewLink": "https://drive.google.com/folder/folder-123",
                    "parents": kwargs["body"].get("parents", []),
                }
            )

        return _CreateRequest(
            {
                "id": "file-123",
                "name": kwargs["body"]["name"],
                "webViewLink": "https://drive.google.com/file/file-123",
            }
        )


class _FakeDriveService:
    def __init__(self):
        self.files_api = _FakeFilesApi()

    def files(self):
        return self.files_api


class _FakeSheetsValuesApi:
    def __init__(self):
        self.update_calls: list[dict] = []

    def update(self, **kwargs):
        self.update_calls.append(kwargs)
        values = kwargs["body"]["values"]
        updated_columns = max((len(row) for row in values), default=0)
        updated_cells = sum(len(row) for row in values)
        return _CreateRequest(
            {
                "updatedRange": kwargs["range"],
                "updatedRows": len(values),
                "updatedColumns": updated_columns,
                "updatedCells": updated_cells,
            }
        )


class _FakeSpreadsheetsApi:
    def __init__(self):
        self.values_api = _FakeSheetsValuesApi()
        self.batch_update_calls: list[dict] = []

    def values(self):
        return self.values_api

    def batchUpdate(self, **kwargs):
        self.batch_update_calls.append(kwargs)
        properties = kwargs["body"]["requests"][0]["addSheet"]["properties"]
        return _CreateRequest(
            {
                "replies": [
                    {
                        "addSheet": {
                            "properties": {
                                "sheetId": 789,
                                "title": properties["title"],
                                "index": properties.get("index", 0),
                                "sheetType": "GRID",
                                "gridProperties": {"rowCount": 1000, "columnCount": 26},
                            }
                        }
                    }
                ]
            }
        )


class _FakeSheetsService:
    def __init__(self):
        self.spreadsheets_api = _FakeSpreadsheetsApi()

    def spreadsheets(self):
        return self.spreadsheets_api


def test_drive_upload_sets_supports_all_drives(tmp_path, monkeypatch):
    upload_file = tmp_path / "example.txt"
    upload_file.write_text("hello")
    fake_service = _FakeDriveService()

    monkeypatch.setattr(client, "get_drive_service", lambda: fake_service)
    monkeypatch.setattr(
        client,
        "MediaFileUpload",
        lambda file_path, mimetype, resumable: {
            "file_path": file_path,
            "mimetype": mimetype,
            "resumable": resumable,
        },
    )

    result = client.drive_upload(str(upload_file), folder_id="parent-123")

    create_call = fake_service.files_api.create_calls[0]
    assert create_call["supportsAllDrives"] is True
    assert create_call["body"]["parents"] == ["parent-123"]
    assert result["id"] == "file-123"
    assert result["name"] == "example.txt"


def test_drive_create_folder_uses_folder_mime_type(monkeypatch):
    fake_service = _FakeDriveService()
    monkeypatch.setattr(client, "get_drive_service", lambda: fake_service)

    result = client.drive_create_folder("Closing Docs", parent_id="parent-123")

    create_call = fake_service.files_api.create_calls[0]
    assert create_call["supportsAllDrives"] is True
    assert create_call["body"] == {
        "name": "Closing Docs",
        "mimeType": "application/vnd.google-apps.folder",
        "parents": ["parent-123"],
    }
    assert result == {
        "id": "folder-123",
        "name": "Closing Docs",
        "web_view_link": "https://drive.google.com/folder/folder-123",
        "parent_ids": ["parent-123"],
    }


def test_sheets_add_tab_uses_batch_update(monkeypatch):
    fake_service = _FakeSheetsService()
    monkeypatch.setattr(client, "get_sheets_service", lambda: fake_service)

    result = client.sheets_add_tab(
        "spreadsheet-123", "Missing From Original List", index=2
    )

    batch_update_call = fake_service.spreadsheets_api.batch_update_calls[0]
    assert batch_update_call == {
        "spreadsheetId": "spreadsheet-123",
        "body": {
            "requests": [
                {
                    "addSheet": {
                        "properties": {"title": "Missing From Original List", "index": 2}
                    }
                }
            ]
        },
    }
    assert result == {
        "spreadsheet_id": "spreadsheet-123",
        "sheet_id": 789,
        "title": "Missing From Original List",
        "index": 2,
        "sheet_type": "GRID",
        "grid_properties": {"rowCount": 1000, "columnCount": 26},
        "sheet_properties": {
            "sheetId": 789,
            "title": "Missing From Original List",
            "index": 2,
            "sheetType": "GRID",
            "gridProperties": {"rowCount": 1000, "columnCount": 26},
        },
        "url": "https://docs.google.com/spreadsheets/d/spreadsheet-123/edit#gid=789",
    }


def test_sheets_write_table_writes_headers_and_rows_to_named_tab(monkeypatch):
    fake_service = _FakeSheetsService()
    monkeypatch.setattr(client, "get_sheets_service", lambda: fake_service)

    result = client.sheets_write_table(
        "spreadsheet-123",
        "Missing From Original's List",
        ["Asset", "Status"],
        [
            {"Asset": "ETH", "Status": "missing"},
            {"Asset": "SOL", "Status": None},
            {"Asset": "ARB"},
        ],
        start_cell="B2",
    )

    update_call = fake_service.spreadsheets_api.values_api.update_calls[0]
    assert update_call == {
        "spreadsheetId": "spreadsheet-123",
        "range": "'Missing From Original''s List'!B2",
        "valueInputOption": "USER_ENTERED",
        "body": {
            "values": [
                ["Asset", "Status"],
                ["ETH", "missing"],
                ["SOL", ""],
                ["ARB", ""],
            ]
        },
    }
    assert result == {
        "spreadsheet_id": "spreadsheet-123",
        "updated_range": "'Missing From Original''s List'!B2",
        "updated_rows": 4,
        "updated_columns": 2,
        "updated_cells": 8,
        "sheet_title": "Missing From Original's List",
        "headers": ["Asset", "Status"],
        "row_count": 3,
        "header_count": 2,
    }
