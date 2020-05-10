# ASGI runserver for django

Install it

```bash
pip install asgi-runserver
```

Add it to the top of your INSTALLED_APPS in your settings

```python
INSTALLED_APPS = [
    'asgi_runserver',
]
```

Run it

```bash
./manage.py runserver --asgi
```