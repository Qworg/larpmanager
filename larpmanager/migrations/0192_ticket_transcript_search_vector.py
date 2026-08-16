# Hand-written migration (W2): drop ``transcript`` from the ticket search-vector
# trigger and recompute every existing ``search_vector`` one final time, keeping
# the legacy ``transcript`` text only for closed tickets so pre-cutover history
# stays searchable until superseded.

from django.contrib.postgres.search import SearchVector
from django.db import migrations
from django.db.models import TextField, Value
from django.db.models.functions import Cast, Coalesce


def recompute_search_vector(apps, schema_editor):
    """Recompute search_vector, including transcript only for closed tickets."""
    LarpManagerTicket = apps.get_model("larpmanager", "LarpManagerTicket")

    base_fields = [
        Cast(Coalesce("subject", Value("")), TextField()),
        Cast(Coalesce("reason", Value("")), TextField()),
        Cast(Coalesce("content", Value("")), TextField()),
        Cast(Coalesce("analysis", Value("")), TextField()),
    ]

    # Closed tickets keep their pre-cutover transcript text searchable one final
    # time; open/working tickets drop it (per-message vectors are the source now).
    LarpManagerTicket.objects.filter(status="done").update(
        search_vector=SearchVector(
            *base_fields,
            Cast(Coalesce("transcript", Value("")), TextField()),
            config="english",
        )
    )
    LarpManagerTicket.objects.exclude(status="done").update(
        search_vector=SearchVector(*base_fields, config="english")
    )


class Migration(migrations.Migration):

    dependencies = [
        ("larpmanager", "0191_larpmanagerticket_last_synced_message_id_and_more"),
    ]

    operations = [
        # Drop the trigger first so the recompute below can set search_vector
        # freely (the row-independent trigger would override the closed-vs-open
        # transcript distinction).
        migrations.RunSQL(
            sql="DROP TRIGGER IF EXISTS larpmanagerticket_search_vector_update ON larpmanager_larpmanagerticket;",
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.RunPython(recompute_search_vector, migrations.RunPython.noop),
        # Replace the function to drop ``transcript`` from the vector concat and
        # recreate the trigger.
        migrations.RunSQL(
            sql="""
            CREATE OR REPLACE FUNCTION larpmanagerticket_search_vector_update() RETURNS trigger AS $$
            BEGIN
                NEW.search_vector := to_tsvector('pg_catalog.english',
                    coalesce(NEW.subject, '') || ' ' ||
                    coalesce(NEW.reason, '') || ' ' ||
                    coalesce(NEW.content, '') || ' ' ||
                    coalesce(NEW.analysis, ''));
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;

            CREATE TRIGGER larpmanagerticket_search_vector_update
            BEFORE INSERT OR UPDATE ON larpmanager_larpmanagerticket
            FOR EACH ROW EXECUTE FUNCTION larpmanagerticket_search_vector_update();
            """,
            reverse_sql="""
            DROP TRIGGER IF EXISTS larpmanagerticket_search_vector_update ON larpmanager_larpmanagerticket;
            CREATE OR REPLACE FUNCTION larpmanagerticket_search_vector_update() RETURNS trigger AS $$
            BEGIN
                NEW.search_vector := to_tsvector('pg_catalog.english',
                    coalesce(NEW.subject, '') || ' ' ||
                    coalesce(NEW.reason, '') || ' ' ||
                    coalesce(NEW.content, '') || ' ' ||
                    coalesce(NEW.analysis, '') || ' ' ||
                    coalesce(NEW.transcript, ''));
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            CREATE TRIGGER larpmanagerticket_search_vector_update
            BEFORE INSERT OR UPDATE ON larpmanager_larpmanagerticket
            FOR EACH ROW EXECUTE FUNCTION larpmanagerticket_search_vector_update();
            """,
        ),
    ]
