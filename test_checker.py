"""
Integration tests for the check_codebase() production path.

test_z3_engine.py covers PactEngine (z3_engine.py).
These tests cover the checker.py → failure_mode.py → encoder.py pipeline,
which is what cli.py actually calls.
"""

import textwrap
from pathlib import Path


from .checker import check_codebase, check_codebase_incremental


def _write_src(tmp_path: Path, filename: str, source: str) -> Path:
    p = tmp_path / filename
    p.write_text(textwrap.dedent(source))
    return p


# ---------------------------------------------------------------------------
# model_constraint violations (REQUIRED_FIELD_MISSING mode)
# ---------------------------------------------------------------------------


def test_clean_create_produces_no_violation(tmp_path):
    _write_src(
        tmp_path,
        "models.py",
        """
        from django.db import models
        class Widget(models.Model):
            name = models.CharField(max_length=64)
            class Meta: app_label = 'x'
    """,
    )
    _write_src(
        tmp_path,
        "views.py",
        """
        from .models import Widget
        def create(org):
            Widget.objects.create(name="foo")
    """,
    )
    violations = check_codebase(tmp_path)
    assert not any(v.call == "Widget.objects.create" for v in violations)


def test_missing_required_field_flagged(tmp_path):
    _write_src(
        tmp_path,
        "models.py",
        """
        from django.db import models
        class Widget(models.Model):
            name = models.CharField(max_length=64)
            class Meta: app_label = 'x'
    """,
    )
    _write_src(
        tmp_path,
        "views.py",
        """
        def create(org):
            Widget.objects.create()
    """,
    )
    violations = check_codebase(tmp_path)
    widget_v = [v for v in violations if v.call == "Widget.objects.create"]
    assert widget_v, "expected model_constraint violation for Widget"
    assert any("name" in m for m in widget_v[0].missing)


def test_pre_extracted_skips_double_parse(tmp_path):
    """Passing _extracted avoids a second extract_from_codebase call."""
    from .extractor import extract_from_codebase

    _write_src(
        tmp_path,
        "models.py",
        """
        from django.db import models
        class Gadget(models.Model):
            sku = models.CharField(max_length=32)
            class Meta: app_label = 'x'
    """,
    )
    _write_src(
        tmp_path,
        "factory.py",
        """
        def make():
            Gadget.objects.create()
    """,
    )
    extracted = extract_from_codebase(tmp_path)
    violations = check_codebase(tmp_path, _extracted=extracted)
    gadget_v = [v for v in violations if v.call == "Gadget.objects.create"]
    assert gadget_v, "pre-extracted path should still find violation"


def test_optional_field_not_flagged(tmp_path):
    _write_src(
        tmp_path,
        "models.py",
        """
        from django.db import models
        class Note(models.Model):
            body = models.TextField(blank=True, null=True)
            class Meta: app_label = 'x'
    """,
    )
    _write_src(
        tmp_path,
        "factory.py",
        """
        def make():
            Note.objects.create()
    """,
    )
    violations = check_codebase(tmp_path)
    assert not any(v.call == "Note.objects.create" for v in violations)


# ---------------------------------------------------------------------------
# required_arg_missing mode
# ---------------------------------------------------------------------------


def test_top_level_function_missing_arg_flagged(tmp_path):
    """Top-level functions (no dot in name) must be checked — regression for
    the removed '.' not in callee_name guard."""
    _write_src(
        tmp_path,
        "lib.py",
        """
        def send_email(to, subject, body):
            pass
    """,
    )
    _write_src(
        tmp_path,
        "usage.py",
        """
        from lib import send_email
        def run():
            send_email("a@b.com", "hello")
    """,
    )
    violations = check_codebase(tmp_path)
    # The call `send_email("a@b.com", "hello")` has 2 positional args but
    # send_email requires 3 — body is missing.
    missing_arg_v = [
        v
        for v in violations
        if v.context == "required_arg_missing" and "send_email" in v.call
    ]
    assert (
        missing_arg_v
    ), "top-level function call missing required arg should be flagged"


def test_kwonly_required_arg_flagged(tmp_path):
    """Keyword-only required args (after *) must be in FunctionManifest."""
    _write_src(
        tmp_path,
        "lib.py",
        """
        def create_user(name, *, role):
            pass
    """,
    )
    _write_src(
        tmp_path,
        "usage.py",
        """
        from lib import create_user
        def run():
            create_user("Alice")
    """,
    )
    violations = check_codebase(tmp_path)
    kwonly_v = [
        v
        for v in violations
        if v.context == "required_arg_missing" and "create_user" in v.call
    ]
    assert kwonly_v, "missing required kwarg-only arg should be flagged"
    assert "role" in kwonly_v[0].missing


def test_star_kwargs_not_flagged_as_missing_arg(tmp_path):
    # Corpus: open-webui — func(**{'key': val, ...}) passes all args via spread.
    # Corpus: vllm — func(**kwargs_var) where kwargs covers required args.
    _write_src(
        tmp_path,
        "lib.py",
        """
        def process(model, texts, url):
            pass
        """,
    )
    _write_src(
        tmp_path,
        "usage.py",
        """
        from lib import process
        def run(model, texts, url):
            process(**{'model': model, 'texts': texts, 'url': url})
            kwargs = {'model': model, 'texts': texts, 'url': url}
            process(**kwargs)
        """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "required_arg_missing"]
    assert not v, "**spread calls must not be flagged as missing required args"


def test_star_args_not_flagged_as_missing_arg(tmp_path):
    # Corpus: vllm — to_bytes(*mm_audio["audio"]) where * unpacks positional args.
    _write_src(
        tmp_path,
        "lib.py",
        """
        def to_bytes(data, sr):
            pass
        """,
    )
    _write_src(
        tmp_path,
        "usage.py",
        """
        from lib import to_bytes
        def run(audio_pair):
            to_bytes(*audio_pair)
        """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "required_arg_missing"]
    assert not v, "*spread positional calls must not be flagged as missing required args"


def test_missing_arg_still_flagged_without_spread(tmp_path):
    # Ensure the true-positive path still fires when no spread is used.
    _write_src(
        tmp_path,
        "lib.py",
        """
        def notify(to, subject, body):
            pass
        """,
    )
    _write_src(
        tmp_path,
        "usage.py",
        """
        from lib import notify
        def run():
            notify("a@b.com", "hi")
        """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "required_arg_missing"]
    assert v, "missing required arg without any spread must still be flagged"


def test_pytest_fixture_call_not_flagged(tmp_path):
    # Corpus: fastapi-users — mock_session_factory(), get_backend_none().
    # In pytest, calling a fixture name in test code invokes its return value
    # (a factory), not the fixture function's full signature.
    _write_src(
        tmp_path,
        "conftest.py",
        """
        import pytest

        @pytest.fixture
        def make_client(db_session, settings):
            def factory(user=None):
                return TestClient(user=user)
            return factory
        """,
    )
    _write_src(
        tmp_path,
        "test_api.py",
        """
        def test_endpoint(make_client):
            client = make_client()
            assert client is not None
        """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "required_arg_missing"]
    assert not v, "@pytest.fixture functions must not be flagged for missing args at call site"


def test_pytest_fixture_with_parens_not_flagged(tmp_path):
    # @pytest.fixture() with explicit call parens is also a fixture.
    _write_src(
        tmp_path,
        "conftest.py",
        """
        import pytest

        @pytest.fixture()
        def db(connection, config):
            return connection.database(config.name)
        """,
    )
    _write_src(
        tmp_path,
        "test_db.py",
        """
        def test_query(db):
            result = db()
            assert result is not None
        """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "required_arg_missing"]
    assert not v, "@pytest.fixture() (with parens) must not be flagged for missing args"


# ---------------------------------------------------------------------------
# bare_except mode
# ---------------------------------------------------------------------------


def test_bare_except_flagged(tmp_path):
    _write_src(
        tmp_path,
        "handler.py",
        """
        def process(data):
            try:
                do_work(data)
            except:
                pass
    """,
    )
    violations = check_codebase(tmp_path)
    bare_v = [v for v in violations if v.context == "bare_except"]
    assert bare_v, "bare except: should be flagged"
    assert any("except:" in v.call for v in bare_v)


def test_silent_except_exception_flagged(tmp_path):
    _write_src(
        tmp_path,
        "handler.py",
        """
        def process(data):
            try:
                do_work(data)
            except Exception:
                pass
    """,
    )
    violations = check_codebase(tmp_path)
    bare_v = [v for v in violations if v.context == "bare_except"]
    assert bare_v, "silent except Exception: pass should be flagged"


def test_except_exception_with_logging_not_flagged(tmp_path):
    _write_src(
        tmp_path,
        "handler.py",
        """
        import logging
        logger = logging.getLogger(__name__)
        def process(data):
            try:
                do_work(data)
            except Exception as exc:
                logger.exception("failed", error=str(exc))
    """,
    )
    violations = check_codebase(tmp_path)
    bare_v = [v for v in violations if v.context == "bare_except"]
    assert not bare_v, "except with logging body should not be flagged"


def test_specific_exception_not_flagged(tmp_path):
    _write_src(
        tmp_path,
        "handler.py",
        """
        def process(data):
            try:
                do_work(data)
            except ValueError:
                pass
    """,
    )
    violations = check_codebase(tmp_path)
    bare_v = [v for v in violations if v.context == "bare_except"]
    assert not bare_v, "specific exception type should not be flagged"


# ---------------------------------------------------------------------------
# save_without_update_fields mode
# ---------------------------------------------------------------------------


def test_save_without_update_fields_flagged(tmp_path):
    _write_src(
        tmp_path,
        "views.py",
        """
        from django.db import models

        def update(obj):
            obj.name = "new"
            obj.save()
    """,
    )
    violations = check_codebase(tmp_path)
    save_v = [v for v in violations if v.context == "save_without_update_fields"]
    assert save_v, "save() without update_fields should be flagged in Django files"


def test_save_with_update_fields_not_flagged(tmp_path):
    _write_src(
        tmp_path,
        "views.py",
        """
        from django.db import models

        def update(obj):
            obj.name = "new"
            obj.save(update_fields=["name"])
    """,
    )
    violations = check_codebase(tmp_path)
    save_v = [v for v in violations if v.context == "save_without_update_fields"]
    assert not save_v, "save(update_fields=[...]) should not be flagged"


def test_form_save_not_flagged(tmp_path):
    _write_src(
        tmp_path,
        "views.py",
        """
        from django.db import models

        def handle(request):
            form = MyForm(request.POST)
            if form.is_valid():
                form.save()
    """,
    )
    violations = check_codebase(tmp_path)
    save_v = [v for v in violations if v.context == "save_without_update_fields"]
    assert not save_v, "form.save() should not be flagged"


def test_compound_serializer_save_not_flagged(tmp_path):
    _write_src(
        tmp_path,
        "views.py",
        """
        from django.db import models

        def update(request, pk):
            user_serializer = UserSerializer(data=request.data)
            if user_serializer.is_valid():
                user_serializer.save()
    """,
    )
    violations = check_codebase(tmp_path)
    save_v = [v for v in violations if v.context == "save_without_update_fields"]
    assert not save_v, "compound *_serializer.save() should not be flagged"


def test_profile_save_is_flagged(tmp_path):
    """'profile'.endswith('file') is True — must NOT be whitelisted by the suffix check."""
    _write_src(
        tmp_path,
        "views.py",
        """
        from django.db import models

        def update_profile(user, name):
            profile = user.profile
            profile.name = name
            profile.save()
    """,
    )
    violations = check_codebase(tmp_path)
    save_v = [v for v in violations if v.context == "save_without_update_fields"]
    assert (
        save_v
    ), "profile.save() should be flagged — 'profile' ends with 'file' but is not a file object"


def test_non_django_save_not_flagged(tmp_path):
    # Corpus: ragas dataset.save(), PIL img.save(path) — not Django models.
    # Fix: require Django import in file before flagging .save().
    _write_src(
        tmp_path,
        "pipeline.py",
        """
        def run(dataset, img, path):
            dataset.save()
            img.save(path)
            experiment.save()
        """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "save_without_update_fields"]
    assert not v, "non-Django .save() calls must not be flagged as missing update_fields"


def test_pil_save_with_positional_not_flagged(tmp_path):
    # img.save("path.png") — positional arg distinguishes from Django .save().
    _write_src(
        tmp_path,
        "render.py",
        """
        from django.db import models

        def export(img, path):
            img.save(path)
        """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "save_without_update_fields"]
    assert not v, "save(path) with positional arg must not be flagged"


def test_django_save_without_update_fields_still_flagged(tmp_path):
    # Django model .save() in a Django file — must still be flagged.
    _write_src(
        tmp_path,
        "views.py",
        """
        from django.db import models

        def update_profile(profile, name):
            profile.name = name
            profile.save()
        """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "save_without_update_fields"]
    assert v, "Django .save() without update_fields must still be flagged in Django files"


def test_new_object_save_not_flagged(tmp_path):
    # Corpus: healthchecks — user = User(...); user.save() is an INSERT, not UPDATE.
    # update_fields is invalid for new (unsaved) objects — Django raises ValueError.
    _write_src(
        tmp_path,
        "views.py",
        """
        from django.db import models

        def register(username, email):
            user = User(username=username, email=email)
            user.set_unusable_password()
            user.save()
        """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "save_without_update_fields"]
    assert not v, "Constructor-then-save (INSERT) must not be flagged — update_fields invalid for new objects"


def test_new_object_save_via_class_attr_not_flagged(tmp_path):
    # check = Check(project=project); check.save() — also an INSERT
    _write_src(
        tmp_path,
        "views.py",
        """
        from django.db import models

        def create_check(project):
            check = Check(project=project)
            check.created = now()
            check.save()
        """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "save_without_update_fields"]
    assert not v, "Check(...) constructor then .save() is an INSERT — must not be flagged"


def test_fetched_object_save_still_flagged(tmp_path):
    # profile = Profile.objects.get(user=user); profile.name = x; profile.save()
    # This IS an UPDATE — should still be flagged.
    _write_src(
        tmp_path,
        "views.py",
        """
        from django.db import models

        def update_name(user_id, name):
            profile = Profile.objects.get(user_id=user_id)
            profile.name = name
            profile.save()
        """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "save_without_update_fields"]
    assert v, "Fetched object .save() (UPDATE) must still be flagged"


def test_save_in_test_file_not_flagged(tmp_path):
    # Corpus: celery/django-celery-beat, healthchecks — 521/1241 (42%) of
    # save_without_update_fields violations are in test_*.py files.
    # Test fixture setup is not subject to concurrent-update races.
    _write_src(
        tmp_path,
        "test_models.py",  # filename starts with test_ → test file
        """
        from django.db import models

        class ScheduleTests:
            def create_model_interval(self, schedule):
                interval = IntervalSchedule.from_schedule(schedule)
                interval.save()  # INSERT fixture — must NOT flag
                return interval

            def test_update(self):
                obj = MyModel.objects.get(pk=1)
                obj.enabled = True
                obj.save()  # test UPDATE — must NOT flag
        """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "save_without_update_fields"]
    assert not v, f"save() in test files must not be flagged: {v}"


def test_save_in_subdir_test_file_not_flagged(tmp_path):
    # Regression: celery/django-celery-beat t/unit/test_schedulers.py leaked through
    # scan_github because temp files lose the original name, making _is_test_file miss
    # basename-prefixed test files nested under non-test directories (t/unit/).
    # The fix: skip test-named files in scan_github *before* writing the temp file.
    # Locally, check_codebase detects test files correctly via basename.
    test_dir = tmp_path / "t" / "unit"
    test_dir.mkdir(parents=True)
    (test_dir / "test_schedulers.py").write_text(
        "import django.db.models\n"
        "def test_update():\n"
        "    m = MyModel.objects.get(pk=1)\n"
        "    m.enabled = True\n"
        "    m.save()\n"
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "save_without_update_fields"]
    assert not v, f"save() in t/unit/test_*.py must not be flagged: {v}"


def test_objects_get_not_flagged_as_optional(tmp_path):
    # Corpus: EvalAI — token = JwtToken.objects.get(user=user); token.refresh_token
    # Model.objects.get() raises DoesNotExist, never returns None.
    _write_src(
        tmp_path,
        "views.py",
        """
        from django.db import models

        def get_token(user):
            token = Token.objects.get(user=user)
            return token.key
        """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "optional_dereference"]
    assert not v, "Model.objects.get() result must not be flagged as optional"


def test_chained_queryset_get_not_flagged_as_optional(tmp_path):
    # Corpus: pythonzm/Ops — server_obj = ServerAssets.objects.select_related('assets').get(id=pk)
    # Chained queryset .select_related().get(), .filter().get(), etc. also raise DoesNotExist.
    _write_src(
        tmp_path,
        "views.py",
        """
        from django.db import models

        def get_server(pk):
            server = Server.objects.select_related('assets').get(id=pk)
            return server.assets.ip

        def get_filtered(pk):
            obj = MyModel.objects.filter(active=True).get(id=pk)
            return obj.name
        """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "optional_dereference"]
    assert not v, "chained queryset .select_related().get() result must not be flagged as optional"


def test_queryset_variable_get_kwargs_only_not_flagged(tmp_path):
    # Corpus: suitenumerique/docs — existing_access = existing_accesses.get(document=entry.document)
    # A queryset stored in a variable then .get(**kwargs) called on it.
    # dict.get() ALWAYS takes a positional key; kwargs-only is always ORM.
    _write_src(
        tmp_path,
        "models.py",
        """
        from django.db import models

        def update_role(existing_accesses, entry, max_role):
            existing_access = existing_accesses.get(document=entry.document)
            existing_access.role = max_role
            existing_access.save(update_fields=['role'])

        def get_by_pk(qs, pk):
            obj = qs.get(pk=pk)
            return obj.name
        """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "optional_dereference"]
    assert not v, f"queryset.get(field=val) must not be flagged as optional: {v}"


def test_django_test_client_get_not_flagged(tmp_path):
    # Corpus: paperless-ngx — response = self.client.get(self.ENDPOINT); response.status_code
    # Django test Client.get() returns HttpResponse, never None.
    _write_src(
        tmp_path,
        "test_api.py",
        """
        class MyTest:
            def test_it(self):
                response = self.client.get(self.ENDPOINT, format="json")
                self.assertEqual(response.status_code, 200)
                return response.data
        """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "optional_dereference"]
    assert not v, "self.client.get() (Django test client) must not be flagged as optional"


def test_tornado_test_client_get_string_concat_not_flagged(tmp_path):
    # Corpus: mher/flower — r = self.get('/api/tasks?' + '&'.join(...))
    # Tornado AsyncHTTPTestCase.get() returns HTTPResponse, never None.
    # String-concat URL: leftmost literal starts with '/'.
    _write_src(
        tmp_path,
        "test_tasks.py",
        """
        import json

        class TaskTest:
            def test_list(self):
                params = dict(limit=4, offset=0)
                r = self.get('/api/tasks?' + '&'.join(
                    '%s=%s' % x for x in params.items()))
                table = json.loads(r.body.decode('utf-8'))
                self.assertEqual(200, r.code)
        """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "optional_dereference"]
    assert not v, "self.get('/path?' + params) Tornado test client must not be flagged as optional"


def test_tornado_url_prefix_concat_not_flagged(tmp_path):
    """mher/flower: self.get(self.url_prefix + '/workers') — rightmost is '/' URL segment.

    27 FPs from flower where leftmost of the BinOp is self.url_prefix (Attribute),
    not a string literal. The rightmost component '/workers' unambiguously identifies
    this as a URL path join, not a dict.get() style optional lookup.
    """
    _write_src(
        tmp_path,
        "test_url_handlers.py",
        """
        import unittest

        class WorkerTest(unittest.TestCase):
            url_prefix = ''

            def test_workers_page(self):
                r = self.get(self.url_prefix + '/workers')
                self.assertEqual(200, r.code)

            def test_root_url(self):
                r = self.get(self.url_prefix + '/')
                self.assertEqual(200, r.code)

            def test_tasks_api(self):
                r = self.get(self.url_prefix + '/api/tasks')
                self.assertEqual(200, r.code)
                import json
                data = json.loads(r.body.decode('utf-8'))
        """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "optional_dereference"]
    assert not v, (
        "self.get(prefix + '/path') Tornado pattern must not be flagged, "
        f"got: {[(x.line, x.call) for x in v]}"
    )


def test_ternary_guard_not_flagged(tmp_path):
    # Corpus: paperless-ngx/suitenumerique — request.user if request else None
    # When `request` is tested as the ternary condition, access in the body is safe.
    _write_src(
        tmp_path,
        "serializers.py",
        """
        def get_user(self):
            request = self.context.get("request")
            user = request.user if request else None
            return user

        def get_version(self):
            request = self.context.get("request")
            return request.version if request else "1.0"
        """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "optional_dereference"]
    assert not v, "x.attr if x else y ternary guard must not be flagged as optional dereference"


def test_unguarded_optional_still_flagged(tmp_path):
    # `dict.get()` without any guard — must still be flagged.
    _write_src(
        tmp_path,
        "views.py",
        """
        def process(d):
            item = d.get("key")
            return item.value
        """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "optional_dereference"]
    assert v, "Unguarded dict.get() result dereference must still be flagged"


# ---------------------------------------------------------------------------
# mutable_default_arg mode
# ---------------------------------------------------------------------------


def test_list_default_flagged(tmp_path):
    _write_src(
        tmp_path,
        "lib.py",
        """
        def append_item(item, items=[]):
            items.append(item)
            return items
    """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "mutable_default_arg"]
    assert v, "list default should be flagged"
    assert any("list" in m for m in v[0].missing)


def test_dict_default_flagged(tmp_path):
    _write_src(
        tmp_path,
        "lib.py",
        """
        def update_cache(key, cache={}):
            cache[key] = True
            return cache
    """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "mutable_default_arg"]
    assert v, "dict default should be flagged"
    assert any("dict" in m for m in v[0].missing)


def test_none_default_not_flagged(tmp_path):
    _write_src(
        tmp_path,
        "lib.py",
        """
        def append_item(item, items=None):
            if items is None:
                items = []
            items.append(item)
            return items
    """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "mutable_default_arg"]
    assert not v, "None default is the correct pattern — should not be flagged"


def test_immutable_default_not_flagged(tmp_path):
    _write_src(
        tmp_path,
        "lib.py",
        """
        def greet(name="world", count=0, flag=True):
            return f"Hello {name}"
    """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "mutable_default_arg"]
    assert not v, "str/int/bool defaults should not be flagged"


def test_readonly_dict_default_not_flagged(tmp_path):
    """reacton/bqplot-style widget API: mutable default passed through but never mutated.

    993 violations in widgetti/reacton were FPs — auto-generated component
    functions use dict/list defaults as configuration specs, never mutated.
    """
    _write_src(
        tmp_path,
        "widgets.py",
        """
        def Axis(label="x", offset: dict = {}, tick_style: dict = {}):
            # read-only: defaults are passed to constructor, never mutated
            return {"label": label, "offset": offset, "tick_style": tick_style}

        def Series(name="series", colors: list = ["steelblue"], interactions: dict = {"hover": "tooltip"}):
            return {"name": name, "colors": colors, "interactions": interactions}
    """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "mutable_default_arg"]
    assert not v, (
        "Read-only mutable defaults (never mutated in function body) "
        f"should not be flagged, got: {[(x.line, x.call) for x in v]}"
    )


def test_mutated_dict_default_still_flagged(tmp_path):
    """Subscript assignment to a dict default IS a real bug — must be flagged."""
    _write_src(
        tmp_path,
        "cache.py",
        """
        def set_flag(key, registry={}):
            registry[key] = True   # mutates the shared default
            return registry
    """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "mutable_default_arg"]
    assert v, "Subscript assignment to dict default must be flagged"


def test_mutated_list_default_via_method_flagged(tmp_path):
    """Calling .append() on a list default IS a real bug — must be flagged."""
    _write_src(
        tmp_path,
        "accum.py",
        """
        def accumulate(item, results=[]):
            results.append(item)   # mutates the shared default
            return results
    """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "mutable_default_arg"]
    assert v, ".append() on list default must be flagged"


# ---------------------------------------------------------------------------
# missing_await mode
# ---------------------------------------------------------------------------


def test_missing_await_flagged(tmp_path):
    _write_src(
        tmp_path,
        "tasks.py",
        """
        import asyncio

        async def fetch_data(url):
            return url

        def start():
            fetch_data("http://example.com")  # missing await
    """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "missing_await"]
    assert v, "unawaited coroutine call should be flagged"
    assert v[0].call == "fetch_data"


def test_awaited_call_not_flagged(tmp_path):
    _write_src(
        tmp_path,
        "tasks.py",
        """
        async def fetch_data(url):
            return url

        async def start():
            result = await fetch_data("http://example.com")
    """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "missing_await"]
    assert not v, "properly awaited call should not be flagged"


def test_sync_function_not_flagged_as_missing_await(tmp_path):
    _write_src(
        tmp_path,
        "lib.py",
        """
        def compute(x):
            return x * 2

        def run():
            result = compute(5)
    """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "missing_await"]
    assert not v, "sync function call should not be flagged as missing await"


def test_drf_view_async_get_method_not_flagged(tmp_path):
    # Regression: async def get(self, request, ...) in a DRF view caused
    # "get" to land in async_funcs, falsely flagging every data.get() call.
    _write_src(
        tmp_path,
        "views.py",
        """
        from rest_framework.views import APIView

        class MyView(APIView):
            async def get(self, request, pk=None):
                return None

        def handler(data):
            name = data.get("name", "default")  # should NOT be flagged
            value = data.get("value")            # should NOT be flagged
    """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "missing_await"]
    assert (
        not v
    ), "dict.get() must not be flagged because async def get(self, ...) exists in same file"


def test_module_level_async_func_without_await_still_flagged(tmp_path):
    # Module-level async function (no self/cls) should still be flagged.
    _write_src(
        tmp_path,
        "tasks.py",
        """
        async def send_email(recipient):
            pass

        def trigger():
            send_email("user@example.com")  # missing await — module-level
    """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "missing_await"]
    assert (
        v
    ), "unawaited module-level async call must still be flagged after scoping fix"
    assert v[0].call == "send_email"


def test_create_task_not_flagged_as_missing_await(tmp_path):
    # Regression: asyncio.create_task(coro()) intentionally does not await — it schedules.
    _write_src(
        tmp_path,
        "conn.py",
        """
        import asyncio

        async def _pump(handler):
            pass

        class Manager:
            async def start(self, handler):
                self._task = asyncio.create_task(_pump(handler))
    """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "missing_await"]
    assert not v, "coroutine passed to create_task must not be flagged"


def test_create_task_prefixed_wrapper_not_flagged_as_missing_await(tmp_path):
    # Regression: user-defined wrappers named create_task_* (e.g. create_task_with_error_handling)
    # consume a coroutine the same way asyncio.create_task does — no await needed.
    _write_src(
        tmp_path,
        "browser.py",
        """
        async def _fetch(url):
            pass

        async def _render():
            pass

        def create_task_with_error_handling(coro, *, name=None):
            import asyncio
            return asyncio.create_task(coro)

        class Session:
            async def load(self, url):
                tasks = {
                    'fetch': create_task_with_error_handling(_fetch(url), name='fetch'),
                    'render': create_task_with_error_handling(_render(), name='render'),
                }
    """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "missing_await"]
    assert not v, "coroutine passed to create_task_* wrapper must not be flagged"


def test_gather_args_not_flagged_as_missing_await(tmp_path):
    # tasks.append(coro()) + asyncio.gather(*tasks) is a standard pattern.
    _write_src(
        tmp_path,
        "runner.py",
        """
        import asyncio

        async def handle_event(event):
            pass

        async def run_all(events):
            tasks = []
            for e in events:
                tasks.append(handle_event(e))
            await asyncio.gather(*tasks)
    """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "missing_await"]
    assert not v, "coroutines appended to gather list must not be flagged"


def test_streaming_response_not_flagged_as_missing_await(tmp_path):
    # StreamingResponse(async_generator()) — framework consumes the coroutine/generator.
    _write_src(
        tmp_path,
        "routes.py",
        """
        async def event_generator():
            yield "data: hello"

        def get_stream():
            return StreamingResponse(event_generator(), media_type="text/event-stream")
    """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "missing_await"]
    assert not v, "async generator passed to StreamingResponse must not be flagged"


def test_asyncio_run_not_flagged_as_missing_await(tmp_path):
    # Regression: asyncio.run(main()) is the canonical entrypoint — not a missing await.
    # 90 of 179 unique missing_await violations in LLM-topic corpus were this pattern.
    _write_src(
        tmp_path,
        "app.py",
        """
        import asyncio

        async def main():
            pass

        if __name__ == '__main__':
            asyncio.run(main())
    """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "missing_await"]
    assert not v, "asyncio.run(coro()) must not be flagged as missing await"


def test_loop_run_until_complete_not_flagged(tmp_path):
    _write_src(
        tmp_path,
        "runner.py",
        """
        import asyncio

        async def setup():
            pass

        loop = asyncio.get_event_loop()
        loop.run_until_complete(setup())
    """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "missing_await"]
    assert not v, "loop.run_until_complete(coro()) must not be flagged"


def test_async_with_context_manager_not_flagged(tmp_path):
    # Regression: async with self._cursor() as cur — _cursor is an async
    # context manager; the call is correct, not a missing await.
    # 61/495 missing_await corpus violations were this pattern.
    _write_src(
        tmp_path,
        "db.py",
        """
        class AsyncDB:
            async def _cursor(self):
                pass

            async def query(self, sql):
                async with self._cursor() as cur:
                    await cur.execute(sql)
        """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "missing_await"]
    assert not v, "async with self._cursor() must not be flagged as missing await"


def test_async_generator_not_flagged_as_missing_await(tmp_path):
    # async def alazy_load(self): yield ... returns AsyncGenerator, not a coroutine.
    # Corpus: open-webui, langchain-ai/langchain — `async for x in self.alazy_load()`.
    _write_src(
        tmp_path,
        "loader.py",
        """
        class Loader:
            async def alazy_load(self):
                yield 1
                yield 2

            async def aload(self):
                return [doc async for doc in self.alazy_load()]
        """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "missing_await"]
    assert not v, "async generator called via async for must not be flagged"


def test_coroutine_assigned_to_task_not_flagged(tmp_path):
    # task = ahandle_event(...) is intentional scheduling; bug pattern is bare expr.
    # Corpus: langchain-ai/langchain callbacks/manager.py — task = ahandle_event(...)
    _write_src(
        tmp_path,
        "callbacks.py",
        """
        import asyncio

        async def ahandle_event(handlers, name):
            for h in handlers:
                await h(name)

        async def dispatch(handlers):
            task = ahandle_event(handlers, "on_start")
            asyncio.ensure_future(task)
        """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "missing_await"]
    assert not v, "coroutine assigned to variable must not be flagged as missing await"


def test_bare_unawaited_coroutine_still_flagged(tmp_path):
    # Expression-statement call with no assignment IS a genuine bug.
    _write_src(
        tmp_path,
        "buggy.py",
        """
        async def save_data():
            pass

        async def run():
            save_data()  # genuine bug: coroutine discarded
        """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "missing_await"]
    assert v, "unawaited bare coroutine call must still be flagged"


def test_async_for_iter_not_flagged(tmp_path):
    # Corpus: langchain — async for chunk in self._astream(...):
    # When a coroutine/async-gen call is the iter of async for, it's consumed correctly.
    _write_src(
        tmp_path,
        "stream.py",
        """
        async def _astream(messages):
            yield messages

        async def run(messages):
            async for chunk in _astream(messages):
                print(chunk)
        """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "missing_await"]
    assert not v, "async for x in coro() must not be flagged as missing await"


def test_async_comprehension_iter_not_flagged(tmp_path):
    # Corpus: langchain — [doc async for doc in self.alazy_load()]
    _write_src(
        tmp_path,
        "loader.py",
        """
        async def alazy_load():
            yield 1

        async def load_all(self):
            return [doc async for doc in alazy_load()]
        """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "missing_await"]
    assert not v, "[x async for x in coro()] must not be flagged as missing await"


def test_await_protocol_not_flagged(tmp_path):
    # Corpus: langchain — def __await__(self): return self._impl().__await__()
    # __await__ implements the awaitable protocol; calling async methods for
    # their generator via .__await__() is correct, not a missing await.
    _write_src(
        tmp_path,
        "awaitable.py",
        """
        import asyncio

        async def _impl():
            return 42

        class MyAwaitable:
            def __await__(self):
                return _impl().__await__()
        """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "missing_await"]
    assert not v, "__await__ protocol impl (coro().__await__()) must not be flagged"


def test_run_worker_not_flagged(tmp_path):
    # Corpus: openai-python — self.run_worker(self.handle_realtime_connection())
    # Textual UI framework: run_worker() intentionally accepts coroutines (schedules them).
    _write_src(
        tmp_path,
        "app.py",
        """
        async def handle_connection(self):
            pass

        class MyApp:
            async def on_mount(self):
                self.run_worker(handle_connection(self))
        """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "missing_await"]
    assert not v, "run_worker(coro) must not be flagged as missing await"


def test_sync_method_calling_shared_name_not_flagged(tmp_path):
    # Corpus: openai-python — SyncClient.close() in def __exit__ while AsyncClient.close()
    # exists in same file. Sync method calling sync method must not be flagged.
    _write_src(
        tmp_path,
        "client.py",
        """
        class SyncClient:
            def __exit__(self, *args):
                self.close()

            def close(self):
                pass

        class AsyncClient:
            async def close(self):
                pass

            async def request(self):
                pass
        """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "missing_await"]
    assert not v, "sync def calling self.method() where async def exists in same file must not be flagged"


def test_async_method_unawaited_still_flagged(tmp_path):
    # Inside async def, self.async_method() without await IS a bug.
    _write_src(
        tmp_path,
        "client.py",
        """
        class AsyncClient:
            async def close(self):
                pass

            async def use(self):
                self.close()  # bug: missing await
        """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "missing_await"]
    assert v, "async def calling self.async_method() without await must still be flagged"


def test_closure_name_collision_not_flagged(tmp_path):
    # Two closures named the same in different outer methods (sync + async).
    # The sync call site must not be flagged (pact can't resolve which is called).
    # Pattern: langchain's ensure_started() — sync def in one method body, async
    # def in another; pact used to flag the sync call because it saw async def.
    _write_src(
        tmp_path,
        "model.py",
        """
        class ChatModel:
            def _stream(self):
                def ensure_started():
                    pass  # sync closure

                def pump():
                    ensure_started()  # calls sync version — must NOT flag

            async def _astream(self):
                async def ensure_started():
                    pass  # async closure in different method

                await ensure_started()
        """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "missing_await"]
    assert not v, f"closure name collision must not be flagged: {v}"


# ---------------------------------------------------------------------------
# format_arg_mismatch mode
# ---------------------------------------------------------------------------


def test_positional_format_mismatch_flagged(tmp_path):
    _write_src(
        tmp_path,
        "lib.py",
        """
        def greet(name):
            msg = "{} {} {}".format(name)
    """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "format_arg_mismatch"]
    assert v, "too few positional args should be flagged"
    assert any("3" in m and "1" in m for m in v[0].missing)


def test_named_format_missing_kwarg_flagged(tmp_path):
    _write_src(
        tmp_path,
        "lib.py",
        """
        def greet():
            msg = "Hello {name}, you are {age} years old".format(name="Alice")
    """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "format_arg_mismatch"]
    assert v, "missing named kwarg should be flagged"
    assert any("age" in m for m in v[0].missing)


def test_correct_format_not_flagged(tmp_path):
    _write_src(
        tmp_path,
        "lib.py",
        """
        def greet(name, age):
            msg = "Hello {}, you are {} years old".format(name, age)
    """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "format_arg_mismatch"]
    assert not v, "correct positional format should not be flagged"


def test_format_with_star_args_not_flagged(tmp_path):
    _write_src(
        tmp_path,
        "lib.py",
        """
        def greet(args):
            msg = "{} {}".format(*args)
    """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "format_arg_mismatch"]
    assert (
        not v
    ), "format with *args splice cannot be statically counted — should not be flagged"


# ---------------------------------------------------------------------------
# llm_response_unguarded mode
# ---------------------------------------------------------------------------


def test_llm_choices_unguarded_flagged(tmp_path):
    _write_src(
        tmp_path,
        "handler.py",
        """
        import openai

        def get_reply(prompt):
            response = openai.chat.completions.create(
                model="gpt-4",
                messages=[{"role": "user", "content": prompt}],
            )
            return response.choices[0].message.content
    """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "llm_response_unguarded"]
    assert v, "unguarded response.choices[0] should be flagged"
    assert "choices" in v[0].call


def test_llm_choices_guarded_not_flagged(tmp_path):
    _write_src(
        tmp_path,
        "handler.py",
        """
        import openai

        def get_reply(prompt):
            response = openai.chat.completions.create(
                model="gpt-4",
                messages=[{"role": "user", "content": prompt}],
            )
            if not response.choices:
                return None
            return response.choices[0].message.content
    """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "llm_response_unguarded"]
    assert not v, "guarded choices access should not be flagged"


def test_llm_choices_ternary_guard_not_flagged(tmp_path):
    # `choices[0] if response.choices else None` is a guarded ternary — not a bug.
    _write_src(
        tmp_path,
        "handler.py",
        """
        import openai

        def get_reply(prompt):
            response = openai.chat.completions.create(
                model="gpt-4",
                messages=[{"role": "user", "content": prompt}],
            )
            choice = response.choices[0] if response.choices else None
            return choice.message.content if choice else ""
    """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "llm_response_unguarded"]
    assert not v, "ternary-guarded choices[0] should not be flagged"


# ---------------------------------------------------------------------------
# unvalidated_lookup_chain
# ---------------------------------------------------------------------------


def test_unvalidated_lookup_chain_flagged(tmp_path):
    _write_src(
        tmp_path,
        "svc.py",
        """
        def route(request, registry):
            handler = registry.get(request.path)
            if handler:
                result = handlers[handler]
                return result
    """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "unvalidated_lookup_chain"]
    assert v, "subscript of get() result without membership check should be flagged"
    assert "handler" in v[0].call


def test_unvalidated_lookup_chain_with_guard_not_flagged(tmp_path):
    _write_src(
        tmp_path,
        "svc.py",
        """
        def route(request, registry, handlers):
            handler = registry.get(request.path)
            if handler and handler in handlers:
                result = handlers[handler]
                return result
    """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "unvalidated_lookup_chain"]
    assert not v, "guarded lookup chain should not be flagged"


def test_unvalidated_lookup_chain_defaultdict_not_flagged(tmp_path):
    """defaultdict[key] after .get() — KeyError impossible, must not be flagged."""
    _write_src(
        tmp_path,
        "svc.py",
        """
        import collections
        def aggregate(records):
            buckets: dict = collections.defaultdict(list)
            for r in records:
                key = r.get("type")
                buckets[key].append(r)
    """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "unvalidated_lookup_chain"]
    assert (
        not v
    ), "defaultdict subscript must not be flagged as unvalidated_lookup_chain"


def test_unvalidated_lookup_chain_annotated_defaultdict_not_flagged(tmp_path):
    """Annotated defaultdict assignment (x: dict = defaultdict(...)) must not flag."""
    _write_src(
        tmp_path,
        "svc.py",
        """
        import collections
        from typing import Any
        def aggregate(records):
            buckets: dict[str, list[Any]] = collections.defaultdict(list)
            for r in records:
                key = r.get("type")
                buckets[key].append(r)
    """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "unvalidated_lookup_chain"]
    assert not v, "annotated defaultdict subscript must not be flagged"


def test_unvalidated_lookup_chain_dict_write_not_flagged(tmp_path):
    """Dict write d[k] = v where k came from .get() must NOT be flagged.
    Corpus: tadata-org/fastapi_mcp — operation_map[operation_id] = {...}
    where operation_id = operation.get('operationId'). Dict writes never raise
    KeyError; also serves as implicit guard for subsequent reads."""
    _write_src(
        tmp_path,
        "convert.py",
        """
        def build_map(operations):
            operation_map = {}
            properties = {}
            for operation in operations:
                operation_id = operation.get('operationId')
                if not operation_id:
                    continue
                # Write — must NOT flag (no KeyError possible on dict write)
                operation_map[operation_id] = {'method': 'get'}
                param_schema = operation.get('schema', {})
                param_name = operation.get('name')
                if param_name:
                    properties[param_name] = param_schema.copy()
                    # Subsequent read after write above — still NOT a bug
                    properties[param_name]['title'] = param_name
    """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "unvalidated_lookup_chain"]
    assert not v, f"dict write d[k]=v must not be flagged as unvalidated_lookup_chain: {v}"


def test_optional_dereference_get_with_default_not_flagged(tmp_path):
    """.get(key, default) with a non-None default must not be flagged."""
    _write_src(
        tmp_path,
        "svc.py",
        """
        def resolve(mapping, key):
            value = mapping.get(key, "unknown")
            return value.upper()
    """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "optional_dereference"]
    assert not v, ".get(key, default) with non-None default must not be optional"


def test_optional_dereference_rhs_self_use_not_flagged(tmp_path):
    """Use of var in the RHS of its own .get() assignment must not be flagged."""
    _write_src(
        tmp_path,
        "svc.py",
        """
        def normalize(short_to_qual, callee):
            if callee not in short_to_qual:
                callee = short_to_qual.get(callee.split('.')[-1], callee)
            return callee
    """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "optional_dereference"]
    assert (
        not v
    ), "callee.split() in the RHS of callee = d.get(callee.split(...)) must not flag"


def test_optional_dereference_http_get_not_flagged(tmp_path):
    """response = client.get('/url/') is an HTTP GET, not dict.get(); must not flag."""
    _write_src(
        tmp_path,
        "test_api.py",
        """
        def test_list(client):
            response = client.get('/api/items/')
            assert response.status_code == 200
    """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "optional_dereference"]
    assert not v, "HTTP client .get('/url/') response must not be flagged as optional"


def test_optional_dereference_http_fstring_get_not_flagged(tmp_path):
    """response = client.get(f'/url/{id}/') with f-string path must not flag."""
    _write_src(
        tmp_path,
        "test_api.py",
        """
        def test_detail(client, item_id):
            response = client.get(f'/api/items/{item_id}/')
            assert response.status_code == 200
    """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "optional_dereference"]
    assert (
        not v
    ), "HTTP client .get(f'/url/{id}/') response must not be flagged as optional"


def test_optional_dereference_requests_get_url_var_not_flagged(tmp_path):
    # Corpus: google/adk-python — response = requests.get(url, headers=h, timeout=60)
    # The url variable and HTTP kwargs distinguish this from dict.get().
    _write_src(
        tmp_path,
        "utils.py",
        """
        import requests

        def fetch(url, headers):
            response = requests.get(url, headers=headers, timeout=60)
            response.raise_for_status()
            return response.json()
        """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "optional_dereference"]
    assert not v, "requests.get(url, timeout=60) response must not be flagged as optional"


def test_optional_dereference_session_get_not_flagged(tmp_path):
    # session.get(url) — HTTP session client, not dict.get()
    _write_src(
        tmp_path,
        "client.py",
        """
        def fetch(session, url):
            response = session.get(url)
            response.raise_for_status()
            return response.json()
        """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "optional_dereference"]
    assert not v, "session.get(url) response must not be flagged as optional"


def test_optional_dereference_dict_get_still_flagged(tmp_path):
    # dict.get(key) returning None and then dereferenced must still be flagged.
    _write_src(
        tmp_path,
        "lookup.py",
        """
        def process(data):
            user = data.get("user")
            return user.name
        """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "optional_dereference"]
    assert v, "dict.get() result used without None check must still be flagged"


def test_optional_dereference_bool_and_guard_not_flagged(tmp_path):
    # `x and x.attr` — short-circuit And guards the attribute access.
    _write_src(
        tmp_path,
        "click.py",
        """
        DEFS = {}

        def handle(tld):
            definition = DEFS.get(tld)
            js_required = definition and definition.render_js
            return js_required
        """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "optional_dereference"]
    assert not v, "x and x.attr short-circuit guard must suppress optional_dereference"


def test_optional_dereference_assert_not_none_guard(tmp_path):
    # `assert x is not None` must permanently guard subsequent uses of x.
    _write_src(
        tmp_path,
        "config.py",
        """
        REGISTRY = {}

        def build(key):
            model = REGISTRY.get(key)
            assert model is not None, "Missing model"
            return model.arch
        """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "optional_dereference"]
    assert not v, "assert x is not None must suppress optional_dereference for x"


def test_optional_dereference_dunder_session_not_flagged(tmp_path):
    # self.__session.get(url) — private HTTP client; response is not Optional.
    _write_src(
        tmp_path,
        "client.py",
        """
        class Client:
            def fetch(self, url):
                r = self.__session.get(url)
                return r.cookies["token"]
        """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "optional_dereference"]
    assert not v, "self.__session.get(url) response must not be flagged as optional"


def test_bare_except_no_callsite_flagged(tmp_path):
    """bare_except now has file_check — catches files with no outgoing calls."""
    _write_src(
        tmp_path,
        "handler.py",
        """
        def process():
            try:
                risky()
            except:
                pass
    """,
    )
    violations = check_codebase(tmp_path)
    v = [v for v in violations if v.context == "bare_except"]
    assert v, "bare_except should fire even in file with no call sites"


def test_vendor_dir_bare_except_not_flagged(tmp_path):
    """Files under vendor/ are third-party code and must be skipped."""
    vendor_dir = tmp_path / "vendor" / "click"
    vendor_dir.mkdir(parents=True)
    (vendor_dir / "_compat.py").write_text(
        "def compat():\n    try:\n        risky()\n    except:\n        pass\n"
    )
    _write_src(tmp_path, "app.py", "def run(): pass\n")
    violations = check_codebase(tmp_path)
    vendor_v = [v for v in violations if "vendor" in v.file]
    assert not vendor_v, f"vendor/ files must be skipped, got: {vendor_v}"


def test_underscore_vendor_dir_not_flagged(tmp_path):
    """Files under _vendor/ are also third-party and must be skipped."""
    vendor_dir = tmp_path / "_vendor" / "requests"
    vendor_dir.mkdir(parents=True)
    (vendor_dir / "utils.py").write_text(
        "def fetch():\n    try:\n        do_io()\n    except:\n        pass\n"
    )
    _write_src(tmp_path, "main.py", "def run(): pass\n")
    violations = check_codebase(tmp_path)
    vendor_v = [v for v in violations if "_vendor" in v.file]
    assert not vendor_v, f"_vendor/ files must be skipped, got: {vendor_v}"


# ---------------------------------------------------------------------------
# check_codebase_incremental — dirty-set propagation
# ---------------------------------------------------------------------------


def test_incremental_unchanged_file_not_analyzed(tmp_path):
    """If models.py is unchanged, violations in its callers are not reported."""
    _write_src(
        tmp_path,
        "models.py",
        """
        from django.db import models
        class Widget(models.Model):
            name = models.CharField(max_length=100)
            color = models.CharField(max_length=50)
    """,
    )
    caller_file = _write_src(
        tmp_path,
        "views.py",
        """
        from models import Widget
        def create_bad():
            Widget.objects.create(name="x")  # missing color — required field
    """,
    )
    # Only views.py is "changed" — should still catch the violation there
    violations, stats = check_codebase_incremental(tmp_path, {str(caller_file)})
    v = [v for v in violations if v.context == "model_constraint"]
    assert v, "violation in dirty views.py should be reported"
    assert stats["dirty_call_sites"] <= stats["total_call_sites"]


def test_incremental_callee_change_marks_caller_dirty(tmp_path):
    """If a callee file changes, its callers must be re-analyzed too."""
    callee_file = _write_src(
        tmp_path,
        "lib.py",
        """
        def process(x, required_arg):
            return x + required_arg
    """,
    )
    _write_src(
        tmp_path,
        "app.py",
        """
        from lib import process
        def run():
            process(1)  # missing required_arg
    """,
    )
    # Only lib.py changed — but app.py calls it, so app.py must be dirty too
    violations, stats = check_codebase_incremental(tmp_path, {str(callee_file)})
    assert (
        stats["dirty_files"] >= 2
    ), "callee change should propagate dirtiness to caller file"


def test_incremental_unchanged_files_skipped(tmp_path):
    """Files not reachable from the dirty set are not included in dirty_files."""
    changed_file = _write_src(
        tmp_path,
        "changed.py",
        """
        def new_func():
            pass
    """,
    )
    _write_src(
        tmp_path,
        "untouched.py",
        """
        def old_func():
            pass
    """,
    )
    _, stats = check_codebase_incremental(tmp_path, {str(changed_file)})
    # dirty_files = 1 (only changed.py); untouched.py is unreachable
    assert (
        stats["dirty_files"] == 1
    ), "untouched.py has no connection to changed.py — should stay clean"


def test_incremental_stats_skip_ratio(tmp_path):
    """skip_ratio should be in [0, 1]."""
    f = _write_src(tmp_path, "a.py", "x = 1")
    _, stats = check_codebase_incremental(tmp_path, {str(f)})
    assert 0.0 <= stats["skip_ratio"] <= 1.0


def test_incremental_full_match_when_all_changed(tmp_path):
    """When all files are dirty, results must match full check_codebase."""
    _write_src(
        tmp_path,
        "models.py",
        """
        from django.db import models
        class Item(models.Model):
            sku = models.CharField(max_length=50)
    """,
    )
    _write_src(
        tmp_path,
        "api.py",
        """
        from models import Item
        def make():
            Item.objects.create()  # missing sku
    """,
    )
    all_files = {str(p) for p in tmp_path.iterdir() if p.suffix == ".py"}
    full = check_codebase(tmp_path)
    incremental, _ = check_codebase_incremental(tmp_path, all_files)
    full_keys = {(v.file, v.line, v.context) for v in full}
    inc_keys = {(v.file, v.line, v.context) for v in incremental}
    assert (
        full_keys == inc_keys
    ), "incremental with all files dirty must match full scan"


def test_overload_stub_mutable_default_not_flagged(tmp_path):
    """@overload stubs never execute — mutable defaults there are FPs.
    The implementation is only flagged if it actually mutates the default.
    """
    _write_src(
        tmp_path,
        "client.py",
        """
        from typing import overload, Literal

        class SyncAPIClient:
            @overload
            def get(self, path: str, *, options: dict = {}, stream: Literal[False] = False) -> str: ...

            @overload
            def get(self, path: str, *, options: dict = {}, stream: Literal[True]) -> bytes: ...

            def get(self, path, *, options: dict = {}, stream=False):
                options["_path"] = path   # mutates the shared default — real bug
                return path

        def caller():
            c = SyncAPIClient()
            c.get("/users")
        """,
    )
    violations = check_codebase(tmp_path)
    mda = [v for v in violations if v.context == "mutable_default_arg" and "client.py" in v.file]
    # Only the implementation (1 violation), not the 2 @overload stubs
    assert len(mda) == 1, f"Expected 1 mutable_default_arg (impl only), got {len(mda)}: {[(v.line, v.call) for v in mda]}"


def test_typing_overload_mutable_default_not_flagged(tmp_path):
    """typing.overload stubs are skipped; implementation flagged only if it mutates the default."""
    _write_src(
        tmp_path,
        "validators.py",
        """
        import typing

        @typing.overload
        def process(items: list = []) -> None: ...

        def process(items: list = []) -> None:
            items.append("sentinel")   # mutates the shared default — real bug

        def caller():
            process()
        """,
    )
    violations = check_codebase(tmp_path)
    mda = [v for v in violations if v.context == "mutable_default_arg" and "validators.py" in v.file]
    assert len(mda) == 1, f"Expected 1 (impl only), got {len(mda)}"


def test_list_comprehension_gather_pattern_not_flagged(tmp_path):
    """tasks = [coro(item) for item in items] is the asyncio batch/gather pattern — not a bug."""
    _write_src(
        tmp_path,
        "batch_runner.py",
        """
        import asyncio

        async def process_item(item):
            return item * 2

        def run_batch(items):
            tasks = [process_item(item) for item in items]
            return asyncio.get_event_loop().run_until_complete(asyncio.gather(*tasks))

        def run_annotated(items):
            tasks: list = [process_item(item) for item in items]
            return asyncio.get_event_loop().run_until_complete(asyncio.gather(*tasks))
        """,
    )
    violations = check_codebase(tmp_path)
    ma = [v for v in violations if v.context == "missing_await" and "batch_runner.py" in v.file]
    assert len(ma) == 0, f"Expected 0 missing_await in list-comp gather pattern, got {len(ma)}: {[(v.line, v.call) for v in ma]}"


def test_gather_star_comprehension_not_flagged(tmp_path):
    """asyncio.gather(*[coro(item) for item in items]) — starred comprehension passed directly."""
    _write_src(
        tmp_path,
        "direct_gather.py",
        """
        import asyncio

        async def fetch(url):
            return url

        async def main(urls):
            results = await asyncio.gather(*[fetch(url) for url in urls])
            return results
        """,
    )
    violations = check_codebase(tmp_path)
    ma = [v for v in violations if v.context == "missing_await" and "direct_gather.py" in v.file]
    assert len(ma) == 0, f"Expected 0 missing_await for starred comprehension, got {len(ma)}: {[(v.line, v.call) for v in ma]}"


def test_list_literal_gather_pattern_not_flagged(tmp_path):
    """tasks = [coro1(), coro2(), coro3()]; await gather(*tasks) — list literal collected for gather."""
    _write_src(
        tmp_path,
        "parallel.py",
        """
        import asyncio

        async def microphone(): pass
        async def emitter(ws): pass
        async def receiver(ws): pass

        async def main(ws):
            tasks = [microphone(), emitter(ws), receiver(ws)]
            await asyncio.gather(*tasks)
        """,
    )
    violations = check_codebase(tmp_path)
    ma = [v for v in violations if v.context == "missing_await"]
    assert len(ma) == 0, f"List literal for gather must not be flagged: {[(v.line, v.call) for v in ma]}"


def test_generator_return_gather_pattern_not_flagged(tmp_path):
    """return (coro(item) for item in items) — lazy generator of coroutines returned to caller."""
    _write_src(
        tmp_path,
        "downloader.py",
        """
        import asyncio

        async def fetch_video(url): pass

        def fetch_all(urls):
            return (fetch_video(url) for url in urls)

        async def main(urls):
            await asyncio.gather(*fetch_all(urls))
        """,
    )
    violations = check_codebase(tmp_path)
    ma = [v for v in violations if v.context == "missing_await"]
    assert len(ma) == 0, f"Generator return of coroutines must not be flagged: {[(v.line, v.call) for v in ma]}"


def test_user_defined_asyncio_run_wrapper_not_flagged(tmp_path):
    """asyncio_run(coro()) — user-defined wrapper around loop.run_until_complete should not be flagged."""
    _write_src(
        tmp_path,
        "main.py",
        """
        import asyncio

        loop = asyncio.get_event_loop()

        def asyncio_run(func):
            loop.run_until_complete(func)

        async def save_data(): pass
        async def load_data(): pass

        def sync_save():
            asyncio_run(save_data())

        def sync_load():
            asyncio_run(load_data())
        """,
    )
    violations = check_codebase(tmp_path)
    ma = [v for v in violations if v.context == "missing_await"]
    assert len(ma) == 0, f"asyncio_run wrapper must not be flagged: {[(v.line, v.call) for v in ma]}"


def test_run_async_wrapper_not_flagged(tmp_path):
    """run_async(coro()) — alternative user-defined wrapper name should not be flagged."""
    _write_src(
        tmp_path,
        "utils.py",
        """
        import asyncio

        def run_async(coro):
            return asyncio.run(coro)

        async def fetch(): pass

        def sync_fetch():
            run_async(fetch())
        """,
    )
    violations = check_codebase(tmp_path)
    ma = [v for v in violations if v.context == "missing_await"]
    assert len(ma) == 0, f"run_async wrapper must not be flagged: {[(v.line, v.call) for v in ma]}"


def test_numba_intrinsic_typingctx_not_counted(tmp_path):
    """Numba @intrinsic functions take typingctx as first param injected by JIT — not user-supplied."""
    _write_src(
        tmp_path,
        "jit_kernels.py",
        """
        from numba.core.extending import intrinsic

        def _get_connection(warehouse, conn_str): ...

        @intrinsic(prefer_literal=True)
        def _get_connection(typingctx, warehouse, conn_str):
            def codegen(context, builder, sig, args): pass
            return None

        def build_connection(warehouse):
            conn_str = "conn"
            return _get_connection(warehouse, conn_str)
        """,
    )
    violations = check_codebase(tmp_path)
    ra = [v for v in violations if v.context == "required_arg_missing"]
    assert len(ra) == 0, f"@intrinsic typingctx must not count toward arity: {[(v.line, v.call) for v in ra]}"


def test_optional_dereference_zero_arg_get_not_flagged(tmp_path):
    """obj.get() with no args is a custom method (e.g. Twisted DeferredQueue), not dict.get().
    dict.get() always requires at least one positional key argument."""
    _write_src(
        tmp_path,
        "twisted_queue.py",
        """
        class DeferredQueue:
            def get(self):
                return Deferred()

        def process(queue):
            d = queue.get()
            d.addCallback(handler)
        """,
    )
    violations = check_codebase(tmp_path)
    od = [v for v in violations if v.context == "optional_dereference" and "twisted_queue.py" in v.file]
    assert len(od) == 0, f"Expected 0 optional_dereference for zero-arg .get(), got {len(od)}: {[(v.line, v.call) for v in od]}"


def test_optional_dereference_pandas_first_not_flagged(tmp_path):
    """pandas groupby().first() returns a DataFrame, not Optional — not a violation."""
    _write_src(
        tmp_path,
        "pandas_agg.py",
        """
        import pandas as pd

        def aggregate(df, key_cols):
            meta = df.set_index(key_cols)[key_cols]
            if not meta.index.is_unique:
                meta = meta.groupby(level=key_cols, dropna=False).first()
            result = meta.index
            return result
        """,
    )
    violations = check_codebase(tmp_path)
    od = [v for v in violations if v.context == "optional_dereference" and "pandas_agg.py" in v.file]
    assert len(od) == 0, f"Expected 0 violations for pandas groupby.first(), got {len(od)}: {[(v.line, v.call) for v in od]}"


def test_click_command_no_args_not_flagged(tmp_path):
    """@click.command() decorated functions get args from CLI — calling main() with no args is correct."""
    _write_src(
        tmp_path,
        "cli_tool.py",
        """
        import click

        @click.command()
        @click.option("--name", required=True)
        @click.option("--count", required=True, type=int)
        def main(name: str, count: int) -> None:
            for _ in range(count):
                print(name)

        if __name__ == "__main__":
            main()
        """,
    )
    violations = check_codebase(tmp_path)
    ra = [v for v in violations if v.context == "required_arg_missing" and "cli_tool.py" in v.file]
    assert len(ra) == 0, f"Expected 0 required_arg_missing for @click.command(), got {len(ra)}: {[(v.line, v.call) for v in ra]}"


def test_app_command_no_args_not_flagged(tmp_path):
    """@app.command() (Typer/Click group) decorated functions should not be flagged."""
    _write_src(
        tmp_path,
        "typer_tool.py",
        """
        import typer
        app = typer.Typer()

        @app.command()
        def deploy(env: str, force: bool = False) -> None:
            pass

        if __name__ == "__main__":
            app()
        """,
    )
    violations = check_codebase(tmp_path)
    ra = [v for v in violations if v.context == "required_arg_missing" and "typer_tool.py" in v.file]
    assert len(ra) == 0, f"Expected 0 required_arg_missing for @app.command(), got {len(ra)}: {[(v.line, v.call) for v in ra]}"


def test_dual_sync_async_client_missing_await_not_flagged(tmp_path):
    """Dual sync/async client in one file: self.close() in async method of AsyncClient
    must not be flagged because the same name exists as sync def in SyncClient."""
    _write_src(
        tmp_path,
        "clients.py",
        """
        import httpx

        class SyncClient:
            def close(self) -> None:
                self._http.close()

            def request(self, method: str, url: str) -> httpx.Response:
                return self._http.request(method, url)

            def _prepare_request(self, method: str) -> dict:
                return {"method": method}

        class AsyncClient:
            async def close(self) -> None:
                await self._http.aclose()

            async def request(self, method: str, url: str) -> httpx.Response:
                self._prepare_request(method)
                return await self._http.request(method, url)

            async def _prepare_request(self, method: str) -> dict:
                return {"method": method}
        """,
    )
    violations = check_codebase(tmp_path)
    ma = [v for v in violations if v.context == "missing_await" and "clients.py" in v.file]
    assert len(ma) == 0, f"Expected 0 missing_await for dual sync/async client, got {len(ma)}: {[(v.line, v.call) for v in ma]}"


def test_main_in_dunder_main_block_not_flagged(tmp_path):
    """`if __name__ == "__main__": main()` must not be flagged as required_arg_missing."""
    _write_src(
        tmp_path,
        "cli_tool.py",
        """
        import argparse

        def main(config: str, verbose: bool = False) -> None:
            print(config, verbose)

        if __name__ == "__main__":
            main()
        """,
    )
    violations = check_codebase(tmp_path)
    ra = [v for v in violations if v.context == "required_arg_missing" and "cli_tool.py" in v.file]
    assert len(ra) == 0, f"Expected 0 required_arg_missing in __main__ block, got {len(ra)}: {[(v.line, v.call) for v in ra]}"


def test_main_called_outside_dunder_block_still_flagged(tmp_path):
    """The same missing-arg call outside __main__ block IS a real bug."""
    _write_src(
        tmp_path,
        "caller.py",
        """
        def main(config: str) -> None:
            print(config)

        def run_pipeline():
            main()  # missing required arg — real bug
        """,
    )
    violations = check_codebase(tmp_path)
    ra = [v for v in violations if v.context == "required_arg_missing" and "caller.py" in v.file]
    assert len(ra) == 1, f"Expected 1 required_arg_missing outside __main__ block, got {len(ra)}"


def test_string_literal_join_not_flagged_as_missing_separator(tmp_path):
    """'sep'.join(items) must not be matched against a user-defined join(items, separator).

    The receiver is a string constant — this is a built-in str.join call, not
    a call to the local join() function.  Returning None from _name() when the
    receiver is a Constant prevents the callee from being resolved to any
    user-defined function that happens to share the method name.
    """
    _write_src(
        tmp_path,
        "helpers.py",
        """
        def join(items, separator):
            return separator.join(items)

        def render(keywords):
            return ", ".join(keywords)   # str.join — NOT a call to the local join()
        """,
    )
    violations = check_codebase(tmp_path)
    ra = [v for v in violations if v.context == "required_arg_missing" and "helpers.py" in v.file]
    assert len(ra) == 0, f"str.join falsely flagged as missing-arg: {ra}"


def test_fstring_join_not_flagged_as_missing_separator(tmp_path):
    """f"...".join(items) must not be matched against a user-defined join(*pairs, separator).

    An f-string receiver is an ast.JoinedStr, not ast.Constant.  The existing
    constant-receiver guard must also cover JoinedStr.

    Corpus evidence: alexpovel/ancv — f" {self.theme.sep} ".join(contact_items)
    was flagged as missing the 'separator' kwarg of the local join() function.
    """
    _write_src(
        tmp_path,
        "templates.py",
        """
        from typing import Optional

        def join(*pairs, separator: str) -> Optional[str]:
            return separator.join(str(p) for p in pairs if p)

        class Theme:
            sep = ' | '

        class Widget:
            def __init__(self):
                self.theme = Theme()

            def render(self, items):
                return f" {self.theme.sep} ".join(items)   # f-string str.join — NOT local join()
        """,
    )
    violations = check_codebase(tmp_path)
    ra = [v for v in violations if v.context == "required_arg_missing" and "templates.py" in v.file]
    assert len(ra) == 0, f"f-string str.join falsely flagged as missing-arg: {ra}"


def test_isinstance_and_attr_not_flagged_as_optional_dereference(tmp_path):
    """isinstance(x, T) and x.attr is safe via short-circuit — must not flag.

    `isinstance(x, T)` returns False when x is None, so the `and` short-circuit
    guarantees x.attr is never reached with x=None.  This is a standard Python
    type-narrowing guard pattern.

    Corpus evidence: neurobionics/onshape-robotics-toolkit — 2 violations of
    `isinstance(instance, AssemblyInstance) and instance.isRigid` where instance
    comes from self.instances.get(key).
    """
    _write_src(
        tmp_path,
        "assembly.py",
        """
        class AssemblyInstance:
            isRigid: bool = True

        class Assembly:
            def __init__(self):
                self.instances = {}

            def is_rigid_assembly(self, key):
                instance = self.instances.get(key)
                return isinstance(instance, AssemblyInstance) and instance.isRigid

            def is_flexible_assembly(self, key):
                instance = self.instances.get(key)
                return isinstance(instance, AssemblyInstance) and not instance.isRigid
        """,
    )
    violations = check_codebase(tmp_path)
    od = [v for v in violations if v.context == "optional_dereference" and "assembly.py" in v.file]
    assert len(od) == 0, f"isinstance guard falsely flagged: {od}"


def test_get_with_keyword_default_not_flagged_as_optional_dereference(tmp_path):
    """xml_element.get('attr', default='fallback') returns a non-None value.

    When .get() is called with a keyword `default=non_None_value`, the result
    is guaranteed non-None.  Using it without a None check must not be flagged.

    Corpus evidence: clemense/yourdfpy — rgba = xml_element.get('rgba', default='1 1 1 1')
    followed by rgba.split() was falsely flagged as optional_dereference.
    """
    _write_src(
        tmp_path,
        "xml_parse.py",
        """
        class XmlElement:
            def get(self, attr, default=None):
                return default

        def parse_color(xml_element):
            rgba = xml_element.get('rgba', default='1 1 1 1')
            return [float(x) for x in rgba.split()]
        """,
    )
    violations = check_codebase(tmp_path)
    od = [v for v in violations if v.context == "optional_dereference" and "xml_parse.py" in v.file]
    assert len(od) == 0, f"get(default=non_None) falsely flagged: {od}"


def test_format_dotted_attr_covered_by_root_kwarg_not_flagged(tmp_path):
    """'{self.tx_ac}'.format(self=obj) must not be flagged as format_arg_mismatch.

    Python's .format() resolves dotted names like {self.tx_ac} by first looking
    up 'self' in kwargs, then accessing .tx_ac on the result.  Pact's regex
    extracts 'self.tx_ac' as the required name, but the actual keyword arg is
    'self'.  The fix: a name 'a.b.c' is considered covered if its root 'a'
    appears in the keyword args.

    Corpus evidence: biocommons/hgvs — 25 violations in alignmentmapper.py where
    '{self.tx_ac}...'.format(self=self, strand_pm=strand_pm) was incorrectly
    flagged as missing self.tx_ac, self.alt_ac, etc.
    """
    _write_src(
        tmp_path,
        "mapper.py",
        """
        class AlignmentMapper:
            def __init__(self, tx_ac, alt_ac):
                self.tx_ac = tx_ac
                self.alt_ac = alt_ac

            def __str__(self):
                strand_pm = '+'
                return (
                    "{self.__class__.__name__}: {self.tx_ac} ~ {self.alt_ac}; "
                    "{strand_pm} strand"
                ).format(self=self, strand_pm=strand_pm)
        """,
    )
    violations = check_codebase(tmp_path)
    fa = [v for v in violations if v.context == "format_arg_mismatch" and "mapper.py" in v.file]
    assert len(fa) == 0, f"dotted attr covered by root kwarg falsely flagged: {fa}"


def test_numba_intrinsic_typingctx_not_flagged(tmp_path):
    """Numba @intrinsic functions have typingctx auto-injected — callers don't pass it.

    bodo-ai/Bodo had 18 required_arg_missing FPs from this pattern:
      def _get_glue_connection(typingctx, warehouse, conn_str): ...
    called as:
      _get_glue_connection(warehouse, conn_str)   # typingctx injected by Numba
    """
    _write_src(
        tmp_path,
        "jit_intrinsics.py",
        """
        def _get_connection(typingctx, warehouse, conn_str):
            # Numba intrinsic: typingctx is auto-injected by the JIT framework
            def codegen(context, builder, signature, args):
                pass
            return None, codegen

        def _run_kernel(typingctx, input_arr, length, output):
            def codegen(context, builder, signature, args):
                pass
            return None, codegen

        def overload_get_connection(warehouse):
            def impl(warehouse):
                conn_str = "conn://" + warehouse
                conn = _get_connection(warehouse, conn_str)   # typingctx injected
                return conn
            return impl

        def overload_run_kernel(input_arr, length):
            def impl(input_arr, length):
                import numpy as np
                output = np.empty(length)
                _run_kernel(input_arr, length, output)        # typingctx injected
                return output
            return impl
        """,
    )
    violations = check_codebase(tmp_path)
    ra = [v for v in violations if v.context == "required_arg_missing"]
    assert len(ra) == 0, (
        "Numba intrinsic calls with typingctx as first param must not be flagged, "
        f"got: {[(v.line, v.call, v.message) for v in ra]}"
    )


def test_bodo_ctx_param_not_flagged(tmp_path):
    """Bodo uses 'ctx' (short form) for the same auto-injected intrinsic parameter.

    bodo-ai/Bodo had 18 required_arg_missing FPs from this pattern:
      def run_crypto_function(ctx, msg, digest_size, output): ...
    called as:
      run_crypto_function(msg, digest_size, output)  # ctx injected by Bodo JIT
    """
    _write_src(
        tmp_path,
        "bodo_intrinsics.py",
        """
        def run_crypto_function(ctx, msg, digest_size, output):
            # Bodo/Numba intrinsic: ctx auto-injected
            pass

        def _get_glue_connection(ctx, warehouse, conn_str):
            pass

        def impl_crypto(msg, digest_size):
            output = "x" * digest_size
            run_crypto_function(msg, digest_size, output)  # ctx auto-injected
            return output

        def impl_glue(warehouse):
            conn_str = "conn://" + warehouse
            conn = _get_glue_connection(warehouse, conn_str)  # ctx auto-injected
            return conn
        """,
    )
    violations = check_codebase(tmp_path)
    ra = [v for v in violations if v.context == "required_arg_missing"]
    assert len(ra) == 0, (
        "Bodo intrinsic calls with 'ctx' as first param must not be flagged, "
        f"got: {[(v.line, v.call, v.message) for v in ra]}"
    )


def test_same_name_closures_different_signatures_not_flagged(tmp_path):
    """Same-named closures in different outer scopes must not produce FPs.

    Corpus evidence: safishamsi/graphify — many `walk` closures defined in
    separate outer functions, each with different signatures.  The checker
    used to pick the wrong `walk` definition and flag calls that were in
    the scope of a *different* walk with an optional (not required) param.
    """
    _write_src(
        tmp_path,
        "graphify.py",
        """
        def extract_classes(root):
            def walk(node, parent_class_nid=None):
                for child in node.children:
                    walk(child, parent_class_nid)  # parent_class_nid optional — NOT a violation

        def extract_imports(root):
            def walk(node, parent_nid: str):       # required param — different signature
                for child in node.children:
                    walk(child, parent_nid)
        """,
    )
    violations = check_codebase(tmp_path)
    ra = [v for v in violations if v.context == "required_arg_missing"]
    assert len(ra) == 0, (
        "Same-named closures with different scopes must not produce required_arg_missing FPs, "
        f"got: {[(v.line, v.call, v.missing) for v in ra]}"
    )


def test_schedule_coroutine_consumer_not_flagged(tmp_path):
    """self.schedule(coro()) must not produce missing_await FP.

    Corpus evidence: langchain-ai/langgraph StreamTransformer.process() calls
    self.schedule(work()) where schedule() accepts Coroutine[Any, Any, Any] and
    wraps it in asyncio.create_task. The coroutine is intentionally not awaited
    at the call site.
    """
    _write_src(
        tmp_path,
        "stream.py",
        """\
        import asyncio

        class StreamTransformer:
            def schedule(self, coro):
                return asyncio.get_event_loop().create_task(coro)

            def process(self, event):
                async def work() -> None:
                    await asyncio.sleep(0.01)
                    print("done")

                self.schedule(work())
                return True
        """,
    )
    violations = check_codebase(tmp_path)
    ma = [v for v in violations if v.context == "missing_await"]
    assert len(ma) == 0, (
        "self.schedule(coro()) must not be flagged as missing_await — "
        "schedule() is a coroutine consumer. "
        f"got: {[(v.line, v.call) for v in ma]}"
    )


def test_optional_deref_suppressed_in_test_files(tmp_path):
    """optional_dereference must not fire in test files.

    Corpus evidence: suitenumerique/docs test_api_documents_threads.py —
    thread = models.Thread.objects.first()  ← optional
    thread.comments.first()                  ← flagged as FP

    In test code, .first() without a null guard is normal assertion style;
    an AttributeError fails the test loudly, which is the desired outcome.
    Production code gets the check; test fixtures do not.
    """
    test_dir = tmp_path / "tests"
    test_dir.mkdir()
    _write_src(
        test_dir,
        "test_api.py",
        """\
        class FakeManager:
            def first(self):
                return None  # returns Optional

        class FakeModel:
            objects = FakeManager()
            comments = FakeManager()

        def test_something():
            # Classic test pattern: .first() without null check
            thread = FakeModel.objects.first()
            comment = thread.comments.first()  # would be flagged in prod, not in test
        """,
    )
    violations = check_codebase(tmp_path)
    od = [v for v in violations if v.context == "optional_dereference"]
    assert len(od) == 0, (
        "optional_dereference must not fire in test files — "
        f"got: {[(v.file, v.line, v.call) for v in od]}"
    )


def test_request_session_save_not_flagged(tmp_path):
    """request.session.save() must not produce save_without_update_fields FP.

    Corpus evidence: allauth/django-celery-results/django-cms (7 entries) —
    request.session.save() is Django's session backend save (SessionBase).
    It does not accept update_fields; flagging it is always a FP.
    """
    _write_src(
        tmp_path,
        "views.py",
        """\
        import django.shortcuts

        def my_view(request):
            request.session['key'] = 'value'
            request.session.save()
        """,
    )
    violations = check_codebase(tmp_path)
    sw = [v for v in violations if v.context == "save_without_update_fields"]
    assert len(sw) == 0, (
        "request.session.save() must not be flagged — it's a session backend save, "
        f"not an ORM model save. got: {[(v.line, v.call) for v in sw]}"
    )


def test_form_input_save_not_flagged(tmp_path):
    """self.input.save() where input is a form must not be flagged.

    Corpus evidence: pennersr/django-allauth headless views (6 entries) —
    self.input is always a Django ModelForm; form.save() is an intentional
    full-model save pattern that does not need update_fields.
    """
    _write_src(
        tmp_path,
        "views.py",
        """\
        import django.shortcuts

        class MyView:
            def post(self, request):
                self.input.save()
        """,
    )
    violations = check_codebase(tmp_path)
    sw = [v for v in violations if v.context == "save_without_update_fields"]
    assert len(sw) == 0, (
        "self.input.save() must not be flagged — 'input' is a form alias, "
        f"not an ORM model. got: {[(v.line, v.call) for v in sw]}"
    )


def test_conftest_and_test_base_not_flagged(tmp_path):
    """conftest.py and test.py must be treated as test files.

    Corpus evidence: healthchecks/hc/test.py (base test class with setUp
    fixtures) produced 10 save_without_update_fields FPs. conftest.py
    (pytest fixture files) similarly contain fixture setup saves.

    Both are test infrastructure, not production code.
    """
    import os
    # conftest.py
    conftest = tmp_path / "conftest.py"
    conftest.write_text(
        "import django\n"
        "class Fixtures:\n"
        "    def setup(self):\n"
        "        self.user.email = 'a@b.com'\n"
        "        self.user.save()\n"
    )
    # test.py (Django base test helper)
    test_base = tmp_path / "test.py"
    test_base.write_text(
        "import django\n"
        "class BaseTest:\n"
        "    def setUp(self):\n"
        "        self.profile.plan = 'free'\n"
        "        self.profile.save()\n"
    )
    violations = check_codebase(tmp_path)
    sw = [v for v in violations if v.context == "save_without_update_fields"]
    assert len(sw) == 0, (
        "conftest.py and test.py must not produce save_without_update_fields — "
        f"they are test infrastructure. got: {[(v.file.split('/')[-1], v.line, v.call) for v in sw]}"
    )


def test_or_null_guard_not_flagged(tmp_path):
    """x is None or x.attr must not produce optional_dereference FP.

    Corpus evidence: google/adk-python mcp_session_manager.py:
      ctx = self._session_contexts.get(session_key)
      ctx_alive = ctx is None or ctx._is_task_alive
    The 'or' short-circuit means ctx._is_task_alive is only evaluated
    when ctx is NOT None. The checker was incorrectly flagging this.
    """
    _write_src(
        tmp_path,
        "session.py",
        """\
        def check(d, key):
            ctx = d.get(key)
            alive = ctx is None or ctx.is_alive   # guarded by Or
            dead = not ctx or ctx.expired          # guarded by not-check Or
            return alive, dead
        """,
    )
    violations = check_codebase(tmp_path)
    od = [v for v in violations if v.context == "optional_dereference"]
    assert len(od) == 0, (
        "x is None or x.attr and not x or x.attr must not be flagged — "
        "the Or short-circuit guards the right-side dereference. "
        f"got: {[(v.line, v.call) for v in od]}"
    )


def test_run_until_complete_any_loop_var_not_flagged(tmp_path):
    """new_loop.run_until_complete(coro()) must not flag missing_await.

    Corpus evidence: AstrBotDevs/AstrBot tests/unit/test_session_lock.py:
      async def get_manager():
          return manager._get_loop_manager()
      return new_loop.run_until_complete(get_manager())
    The checker only whitelisted ('loop', 'run_until_complete') — a receiver
    named 'new_loop', 'event_loop', etc. was incorrectly flagged.
    """
    _write_src(
        tmp_path,
        "thread_runner.py",
        """\
        import asyncio

        async def fetch():
            pass

        def run_in_thread():
            new_loop = asyncio.new_event_loop()
            try:
                return new_loop.run_until_complete(fetch())
            finally:
                new_loop.close()
        """,
    )
    violations = check_codebase(tmp_path)
    ma = [v for v in violations if v.context == "missing_await"]
    assert len(ma) == 0, (
        "new_loop.run_until_complete(coro()) must not be flagged — "
        "run_until_complete accepts a coroutine object, not an awaited value. "
        f"got: {[(v.line, v.call) for v in ma]}"
    )


def test_ts_return_async_call_not_flagged(tmp_path):
    """return asyncFn() in an async TS method must not be flagged as missing_await.

    Corpus evidence: safishamsi/graphify tests/fixtures/sample.ts:
      async get(path: string): Promise<Response> {
          return fetch(this.baseUrl + path);  // valid — propagates Promise
      }
      async post(path: string, body: unknown): Promise<Response> {
          return this.get(path);  // valid — propagates Promise
      }
    Returning a Promise from an async function is intentional; the caller
    is responsible for awaiting. Flagging this produces FPs in any wrapper
    that returns a typed Promise<T>.
    """
    from .ts_checker import check_ts_file

    ts_file = tmp_path / "client.ts"
    ts_file.write_text(
        """\
class HttpClient {
    private baseUrl: string;
    constructor(baseUrl: string) { this.baseUrl = baseUrl; }
    async get(path: string): Promise<Response> {
        return fetch(this.baseUrl + path);
    }
    async post(path: string, body: unknown): Promise<Response> {
        return this.get(path);
    }
}
"""
    )
    violations = check_ts_file(str(ts_file))
    ma = [v for v in violations if v.context == "missing_await"]
    assert len(ma) == 0, (
        "return asyncFn() must not be flagged — it propagates the Promise. "
        f"got: {[(v.line, v.call) for v in ma]}"
    )
