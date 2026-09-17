# Generated for Cloud API Meta provider choice

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("crm_app", "0207_venda_pedido_pap"),
    ]

    operations = [
        migrations.AlterField(
            model_name="whatsappintegracaoconfig",
            name="provider",
            field=models.CharField(
                choices=[
                    ("zapi", "Z-API (legado / plano B)"),
                    ("evolution", "Evolution + n8n (Opção B)"),
                    ("whatsatende", "WhatsAtende (A+B)"),
                    (
                        "hybrid",
                        "Híbrido: Z-API (equipe) + WhatsAtende ou Cloud API Meta (cliente)",
                    ),
                    (
                        "meta",
                        "Cloud API Meta: Z-API (equipe, se houver) + Graph API (cliente)",
                    ),
                ],
                default="zapi",
                max_length=20,
                verbose_name="Provedor ativo",
            ),
        ),
    ]
