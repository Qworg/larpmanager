# Generated migration for Discord ticket integration

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('larpmanager', '0126_fix_event_slug_unique_constraint'),
    ]

    operations = [
        migrations.AddField(
            model_name='larpmanagerticket',
            name='discord_channel_id',
            field=models.BigIntegerField(
                blank=True,
                db_index=True,
                help_text='The Discord channel ID where this ticket conversation takes place',
                null=True,
                unique=True,
                verbose_name='Discord Channel ID',
            ),
        ),
        migrations.AddField(
            model_name='larpmanagerticket',
            name='discord_creator_id',
            field=models.BigIntegerField(
                blank=True,
                db_index=True,
                help_text='The Discord user ID of the ticket creator',
                null=True,
                verbose_name='Discord Creator ID',
            ),
        ),
        migrations.AddField(
            model_name='larpmanagerticket',
            name='assigned_staff_discord_id',
            field=models.BigIntegerField(
                blank=True,
                help_text='The Discord user ID of the assigned staff member',
                null=True,
                verbose_name='Assigned Staff Discord ID',
            ),
        ),
        migrations.AddField(
            model_name='larpmanagerticket',
            name='subject',
            field=models.CharField(
                blank=True,
                help_text='Short subject line for the ticket',
                max_length=255,
                null=True,
                verbose_name='Subject',
            ),
        ),
        migrations.AddField(
            model_name='larpmanagerticket',
            name='transcript',
            field=models.TextField(
                blank=True,
                help_text='Full conversation transcript from Discord',
                null=True,
                verbose_name='Transcript',
            ),
        ),
        migrations.AddField(
            model_name='larpmanagerticket',
            name='closed_at',
            field=models.DateTimeField(
                blank=True,
                help_text='Timestamp when the ticket was closed',
                null=True,
                verbose_name='Closed at',
            ),
        ),
    ]
