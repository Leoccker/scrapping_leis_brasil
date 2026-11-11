"""Teste offline do parser. Rodar: python -m unittest"""
import os
import unittest

import leis

AMOSTRA = os.path.join(os.path.dirname(__file__), "tests", "amostra.html")


class ParserTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(AMOSTRA, encoding="utf-8") as f:
            cls.doc = leis.parse(leis.extrair_paragrafos(f.read()))
        cls.art = {a["numero"]: a for a in cls.doc["artigos"]}

    def test_artigos_ignoram_texto_entre_aspas(self):
        # o "Art. 5º" e o "§ 1º" entre aspas pertencem à lei alterada, não a esta
        self.assertEqual(list(self.art), ["1º", "2º", "3º", "4º", "5º", "9º", "10", "10-A"])
        self.assertEqual([d["tipo"] for d in self.art["3º"]["dispositivos"]], ["caput"])
        self.assertIn("“Art. 5º Novo texto", self.art["3º"]["texto"])

    def test_casos_dificeis(self):
        # termo “citado” dentro do bloco não fecha as aspas; vários parênteses depois do fecho, sim
        self.assertEqual(len(self.art["4º"]["dispositivos"]), 1)
        self.assertIn("Art. 7º Artigo ainda dentro", self.art["4º"]["texto"])
        d = self.art["5º"]["dispositivos"]
        self.assertFalse(d[0]["revogado"])  # tachado parcial não revoga o artigo
        self.assertIn("CVM - Comissão", d[0]["texto"])  # sigla não é inciso
        self.assertEqual([x["rotulo"] for x in d], ["Art. 5º", "§ 1º"])  # "§ 1º Vetado" não vira rubrica
        self.assertIsNone(self.art["9º"]["rubrica"])

    def test_hierarquia(self):
        self.assertEqual(self.art["2º"]["caminho"], ["Capítulo I - DAS DISPOSIÇÕES GERAIS"])
        self.assertEqual(self.art["3º"]["caminho"], ["Capítulo II - DAS ALTERAÇÕES", "Seção I - Da Lei Alterada"])

    def test_dispositivos(self):
        d = self.art["2º"]["dispositivos"]
        self.assertEqual([x["tipo"] for x in d],
                         ["caput", "inciso", "inciso", "alinea", "alinea", "inciso", "paragrafo", "paragrafo"])
        self.assertEqual([x["rotulo"] for x in d], ["Art. 2º", "I", "II", "a)", "b)", "III", "§ 1º", "§ 2º"])
        self.assertEqual([x["revogado"] for x in d][-2:], [True, True])  # "(Revogado...)" e tachado
        self.assertEqual(self.art["1º"]["dispositivos"][1]["rotulo"], "Parágrafo único")

    def test_rubrica_preambulo_rodape(self):
        self.assertEqual(self.art["10"]["rubrica"], "Rubrica do décimo")
        self.assertNotIn("Rubrica", self.art["9º"]["texto"])
        self.assertTrue(self.doc["preambulo"].startswith("LEI Nº 1"))  # cabeçalho do órgão descartado
        self.assertEqual(self.doc["rodape"], "Brasília, 1º de janeiro de 2000; 179º da Independência.\nFULANO DE TAL")
        self.assertEqual(self.art["10-A"]["texto"], "Art. 10-A. Artigo acrescido.")

    def test_texto_limpo(self):
        t = self.doc["texto_integral"]
        self.assertNotIn("\xa0", t)
        self.assertNotIn("margin", t)  # conteúdo de <style>

    def test_markdown(self):
        md = leis.render_md({"nome": "Lei nº 1", "ementa": "Dispõe.", "url": "u", "versao": "compilada", **self.doc})
        self.assertIn("## Capítulo I - DAS DISPOSIÇÕES GERAIS", md)
        self.assertIn("### Seção I - Da Lei Alterada", md)
        self.assertIn("**Art. 2º** A Administração observará:", md)
        self.assertIn("\n    - a) a pública;\n", md)
        self.assertIn("- § 2º ~~Texto antigo tachado.~~", md)

    def test_dados_remotos_nao_escapam(self):
        p = leis.caminho_saida("o", "json", {"id": "1", "tipo": "..", "anoassinatura": "../x", "norma": "a/b"}, "json")
        self.assertEqual(os.path.normpath(p), p)
        self.assertTrue(p.startswith(os.path.join("o", "json") + os.sep))
        self.assertTrue(leis._url_oficial("https://normas.leg.br/api/public/binario/x/texto"))
        for ruim in ("file:///etc/passwd", "http://normas.leg.br/x", "https://169.254.169.254/", "https://normas.leg.br.evil.com/"):
            self.assertFalse(leis._url_oficial(ruim))

    def test_escolher_versoes(self):
        meta = {"encoding": [
            {"version": "Intermediate", "contentUrl": "r", "name": "Retificação", "datePublished": "2021-01-01"},
            {"version": "Original", "contentUrl": "o", "name": "Publicação Original"},
            {"version": "Current", "contentUrl": "en", "name": "Compilação Traduzida [Inglês]"},
            {"version": "Current", "contentUrl": "c", "name": "Compilação Monovigente"}]}
        self.assertEqual([v["contentUrl"] for v in leis.escolher_versoes(meta)], ["c", "o", "r"])
        self.assertEqual([v["contentUrl"] for v in leis.escolher_versoes(
            {"encoding": {"version": "original", "contentUrl": "x"}})], ["x"])
        self.assertEqual(leis.escolher_versoes({}), [])


if __name__ == "__main__":
    unittest.main()
