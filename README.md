# Firmware-Cloud

Web app nho de quan ly luu tru: upload, download, xoa, thung rac, va cap nhat dung luong.

## Chay local

1. Tao moi truong ao va cai thu vien:

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

2. Chay ung dung:

```
python app.py
```

Mo trinh duyet tai http://localhost:5000

Neu muon ma hoa file, dat bien moi truong `ENCRYPTION_KEY` theo dinh dang Fernet.

## Chay voi Docker

```
docker compose up --build
```

Mo trinh duyet tai http://localhost:5000
