"""Geração de PDF a partir de HTML — a metade CPU do caso de uso "HTML → PDF".

Sem env/rede/FastAPI (mesma disciplina de document_extract.py e
content_policy.py): importável pelos testes sem as env vars obrigatórias do
main.py. Quem traduz as exceções daqui em status HTTP é o handler.

---------------------------------------------------------------------------
Por que a renderização acontece AQUI e não no pod
---------------------------------------------------------------------------
O HTML já chega pronto do cliente (normalmente produzido pelo próprio modelo
numa resposta de chat comum) — não há inferência envolvida em transformar
esse HTML num PDF, é puro trabalho de layout/rasterização, e isso é CPU, não
GPU. O gateway já roda em CPU no Railway (ver Dockerfile), separado dos pods,
então cai no mesmo lugar que o parsing/OCR de document_extract.py.

A consequência a NÃO esquecer: renderizar é síncrono e pode ser pesado (CSS
complexo, muitas páginas), e o gateway é um processo único que também atende
todo o tráfego de chat. Por isso render_pdf é uma função bloqueante comum, e
o handler é obrigado a chamá-la via asyncio.to_thread.

---------------------------------------------------------------------------
Por que o url_fetcher é bloqueado
---------------------------------------------------------------------------
O WeasyPrint, por padrão, busca na rede qualquer recurso referenciado no HTML
(<img src="http://...">, @import, fontes remotas). O gateway roda na rede
privada do Railway com acesso a Supabase/RunPod — um HTML malicioso vindo de
um cliente autenticado poderia usar isso como SSRF (ex.: apontar para um
serviço interno ou para o metadata endpoint de uma cloud). Por isso o HTML
recebido aqui é tratado como não confiável e só recursos embutidos (data:
URIs) são aceitos; qualquer outra URL é recusada antes de qualquer tentativa
de rede.
"""

import io
import re

# Tetos por plano. Dois eixos, como em document_extract.py: bytes de entrada
# protegem memória/transferência do HTML recebido, páginas do PDF resultante
# protegem CPU (é o número de páginas renderizadas, não o tamanho do HTML,
# que decide o custo de layout — um HTML de 10 KB com CSS de page-break em
# loop pode gerar milhares de páginas).
#
# Plano ausente do dict cai no default conservador: um plano novo nunca herda
# um teto alto por esquecimento.
#
# "VibeCoder" é o nome antigo de "Go" — entrada mantida só até a migration
# 0049 rodar em produção; sem ela o plano cairia no default na janela de
# transição (mesmo raciocínio de document_extract.py).
# Enterprise é negociado por contrato ("Custom" na página de preços); os
# valores abaixo são o ponto de partida aplicado até o contrato pedir mais —
# ver mesma observação em document_extract.py.
MAX_HTML_BYTES = {
    "Go": 2 * 1024 * 1024,
    "VibeCoder": 2 * 1024 * 1024,
    "Pro": 5 * 1024 * 1024,
    "Max": 8 * 1024 * 1024,
    "Enterprise": 15 * 1024 * 1024,
}
MAX_PDF_PAGES = {
    "Go": 20,
    "VibeCoder": 20,
    "Pro": 50,
    "Max": 75,
    "Enterprise": 150,
}
DEFAULT_MAX_HTML_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_PDF_PAGES = 20


class DocumentGenerateError(Exception):
    """Base das falhas de geração — o handler mapeia para 4xx."""


class HtmlTooLarge(DocumentGenerateError):
    """HTML de entrada excedeu o teto do plano."""


class TooManyPages(DocumentGenerateError):
    """O layout resultante excedeu o teto de páginas do plano."""


class RenderError(DocumentGenerateError):
    """HTML/CSS não pôde ser renderizado (malformado, ou erro interno do
    motor de renderização)."""


def limit_bytes(plan: str | None) -> int:
    return MAX_HTML_BYTES.get(plan or "", DEFAULT_MAX_HTML_BYTES)


def max_limit_bytes() -> int:
    """O teto de bytes mais alto entre todos os planos.

    Mesmo papel de document_extract.max_limit_bytes(): o corte grosso que o
    middleware de Content-Length aplica antes de saber o plano de quem está
    chamando."""
    return max([*MAX_HTML_BYTES.values(), DEFAULT_MAX_HTML_BYTES])


def limit_pages(plan: str | None) -> int:
    return MAX_PDF_PAGES.get(plan or "", DEFAULT_MAX_PDF_PAGES)


def check_size(size: int, plan: str | None) -> None:
    """Teto de bytes do HTML de entrada, checado antes de qualquer parsing."""
    ceiling = limit_bytes(plan)
    if size > ceiling:
        raise HtmlTooLarge(
            f"HTML excede o limite deste plano "
            f"({size // 1024} KB > {ceiling // 1024} KB)"
        )


def _url_fetcher():
    """Único url_fetcher aceito para HTML de cliente: só data: URIs
    embutidos, nada de rede. Sem isso o WeasyPrint sairia buscando na rede
    qualquer http(s)/file referenciado no HTML — ver o cabeçalho do módulo.
    `allowed_protocols` é nativo do WeasyPrint (>= 63) para isto."""
    from weasyprint.urls import URLFetcher

    return URLFetcher(allowed_protocols={"data"})


# Instrução fixa da geração via modelo — mesmo papel de EXTRACTION_PROMPT em
# document_extract.py: garante que o texto pedido pelo cliente (`user`) chega
# ao modelo ACOMPANHADO das regras que o resto do pipeline depende (HTML
# autossuficiente, sem markdown em volta). Sem "só o HTML, nada mais", o
# modelo tende a devolver um bloco ```html ... ``` com explicação antes/depois
# — e isso quebraria o render (WeasyPrint trataria os backticks como texto).
GENERATION_PROMPT = (
    "Gere um documento HTML completo e autossuficiente para ser convertido em PDF. "
    "Regras obrigatórias: (1) devolva APENAS o HTML, sem explicação antes ou depois, "
    "sem bloco de código markdown (```); (2) o HTML não pode referenciar nenhum "
    "recurso externo (sem <img src=\"http://...\">, sem @import, sem fontes remotas) "
    "— se precisar de imagem, embuta como data: URI; (3) use CSS inline ou em <style> "
    "dentro do próprio HTML, nunca um <link> externo."
)

USER_INSTRUCTION_BLOCK = "\n\n--- O QUE GERAR ---\n{user}\n--- FIM ---"

# Um modelo que ignora a regra "só o HTML" e ainda assim envolve a resposta
# num bloco markdown é comum o suficiente pra valer uma rede de proteção aqui,
# em vez de confiar só na instrução do prompt.
#
# search() (não match() ancorado no texto inteiro) de propósito: um modelo que
# ignora a instrução costuma ignorar ela PARCIALMENTE — o bloco ```html ... ```
# vem certo, mas cercado de prosa antes ("Aqui está o documento:") ou depois
# ("Espero que ajude!"). Ancorar com ^...$ no texto inteiro só pega o caso raro
# em que a resposta É o fence do início ao fim, e devolve a prosa+fence intacta
# em todos os outros — exatamente o caso mais comum que esta função existe pra
# cobrir. Sem fence nenhum no texto (HTML cru, como a instrução pede), o
# search() não acha nada e devolve o texto original sem alteração.
_FENCE_RE = re.compile(r"```(?:html)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def strip_html_fences(text: str) -> str:
    """Remove um bloco ```html ... ``` (ou ``` ... ```) em volta do HTML —
    incluindo qualquer prosa antes/depois do bloco — se o modelo tiver
    ignorado a instrução de devolver só o HTML cru."""
    match = _FENCE_RE.search(text)
    return match.group(1).strip() if match else text


def build_messages(user_instruction: str) -> list[dict]:
    """Mensagem `user` da geração: instrução padrão (regras do HTML) + o que
    o cliente pediu. Mesma composição de document_extract.build_messages —
    `user_instruction` SOMA à instrução padrão, nunca a substitui, senão o
    cliente perderia sem querer a garantia de "só HTML, sem recurso externo"
    que o resto do pipeline (render_pdf) depende para não falhar depois."""
    return [{
        "role": "user",
        "content": GENERATION_PROMPT + USER_INSTRUCTION_BLOCK.format(user=user_instruction.strip()),
    }]


def render_pdf(html: str, plan: str | None) -> bytes:
    """Devolve os bytes do PDF renderizado a partir do HTML.

    BLOQUEANTE por design (WeasyPrint é síncrono e usa CPU). Chamar SEMPRE
    via asyncio.to_thread — ver o cabeçalho do módulo.

    O teto de páginas é checado depois de render() mas antes de write_pdf():
    o WeasyPrint separa o layout (lista de páginas) da rasterização final do
    PDF, então um documento com páginas demais é rejeitado sem pagar o custo
    de serializar o PDF inteiro."""
    try:
        from weasyprint import HTML
    except ImportError as e:  # pragma: no cover - ambiente sem a dependência
        raise DocumentGenerateError("suporte a geração de PDF indisponível no servidor") from e

    try:
        document = HTML(string=html, url_fetcher=_url_fetcher()).render()
    except Exception as e:
        raise RenderError(f"não foi possível renderizar o HTML: {str(e)[:300]}") from e

    ceiling = limit_pages(plan)
    if len(document.pages) > ceiling:
        raise TooManyPages(
            f"o documento gerado teria {len(document.pages)} página(s), "
            f"o limite deste plano é {ceiling}"
        )

    buffer = io.BytesIO()
    try:
        # etapa separada de render(): pode falhar por motivos que só aparecem
        # na rasterização final (ex.: um data: URI que passa no parsing do
        # layout mas quebra ao decodificar/desenhar a imagem). Sem este
        # try/except a exceção subia crua — sem DocumentGenerateError pra
        # _render_pdf_guarded capturar, e sem log_gateway_request nenhum no
        # caminho de erro (buraco de observabilidade, virava 500 opaco).
        document.write_pdf(target=buffer)
    except Exception as e:
        raise RenderError(f"não foi possível gerar o PDF: {str(e)[:300]}") from e
    return buffer.getvalue()
