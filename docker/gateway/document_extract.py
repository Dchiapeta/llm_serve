"""Extração de texto de PDF — a metade CPU do caso de uso "documento → JSON".

Sem env/rede/FastAPI (mesma disciplina de content_policy.py e
context_budget.py): importável pelos testes sem as env vars obrigatórias do
main.py. Quem traduz as exceções daqui em status HTTP é o handler.

---------------------------------------------------------------------------
Por que a extração acontece AQUI e não no pod
---------------------------------------------------------------------------
O modelo servido nos pods é texto puro (Qwen3.x, sem variante VL) — ele não
tem como receber os bytes de um PDF. Então o produto "manda o PDF, recebe o
JSON" só existe se alguém converter o documento em texto antes da inferência,
e esse alguém é o gateway: ele já roda em CPU no Railway, separado dos pods
GPU (ver docker/gateway/Dockerfile), e parsing/OCR é exatamente trabalho de
CPU. Nada disso encosta na GPU.

A consequência a NÃO esquecer: OCR é síncrono e pesado, e o gateway é um
processo único que também atende todo o tráfego de chat. Rodar OCR inline no
event loop trava as requisições de TODOS os outros clientes enquanto o
tesseract mói uma página. Por isso extract_text é uma função bloqueante
comum, e o handler é obrigado a chamá-la via asyncio.to_thread.

Os limites por plano abaixo são o que mantém esse custo previsível: uma
requisição só pode consumir CPU proporcional ao plano do cliente, e a
rejeição acontece ANTES do OCR (custo zero pra documento que não passa).
"""

import io

# Tetos por plano. Dois eixos porque medem custos diferentes: bytes protege
# memória/transferência (um PDF de 100 MB não deve nem ser aberto), páginas
# protegem CPU (é o número de páginas, não o tamanho do arquivo, que decide
# quantas vezes o tesseract vai rodar — um PDF escaneado de 2 MB pode custar
# mais CPU que um digital de 10 MB).
#
# Plano ausente do dict cai no default conservador: um plano novo nunca herda
# um teto alto por esquecimento.
#
# "VibeCoder" é o nome antigo de "Go" — entrada mantida só até a migration 0049
# rodar em produção; sem ela o plano cairia no default na janela de transição.
# Enterprise é negociado por contrato ("Custom" na página de preços); os
# valores abaixo são o ponto de partida aplicado até o contrato pedir mais —
# não há ainda um mecanismo de override por conta, então um teto maior exige
# alterar esta constante manualmente.
MAX_DOCUMENT_BYTES = {
    "Go": 8 * 1024 * 1024,
    "VibeCoder": 8 * 1024 * 1024,
    "Pro": 15 * 1024 * 1024,
    "Max": 25 * 1024 * 1024,
    "Enterprise": 50 * 1024 * 1024,
}
MAX_DOCUMENT_PAGES = {
    "Go": 15,
    "VibeCoder": 15,
    "Pro": 30,
    "Max": 50,
    "Enterprise": 100,
}
DEFAULT_MAX_DOCUMENT_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_DOCUMENT_PAGES = 15

# Tetos de imagem solta (não embutida em PDF). Bytes por plano — mais baixos
# que os de PDF porque uma imagem é sempre "1 página": não há como um upload
# grande justificar custo maior de rede/memória do que o teto de PDF do mesmo
# plano. Megapixels protege CPU (é o número de pixels, não os bytes, que
# decide o custo do tesseract — um JPEG bem comprimido pode decodificar numa
# imagem de resolução absurda e custar CPU equivalente a várias páginas de
# PDF, mesmo pesando pouco em disco).
MAX_IMAGE_BYTES = {
    "Go": 5 * 1024 * 1024,
    "VibeCoder": 5 * 1024 * 1024,
    "Pro": 10 * 1024 * 1024,
    "Max": 15 * 1024 * 1024,
    "Enterprise": 25 * 1024 * 1024,
}
DEFAULT_MAX_IMAGE_BYTES = 5 * 1024 * 1024
# 20 MP cobre com folga qualquer foto de celular/scan de documento; acima
# disso o custo de decode+OCR passa de "uma página" pra vários segundos de
# CPU só pra abrir o arquivo.
MAX_IMAGE_MEGAPIXELS = 20_000_000
# Formatos que o Pillow/pytesseract leem sem plugin externo e que cobrem o
# caso de uso real (foto de celular, print de tela, scan avulso).
SUPPORTED_IMAGE_FORMATS = {"JPEG", "PNG", "WEBP"}

# DPI do render pro OCR. 200 é o meio caminho conhecido: abaixo de ~150 o
# tesseract erra dígitos em fonte pequena (justamente o que mais importa em
# nota fiscal/CNPJ), e acima de ~300 o custo de CPU sobe sem ganho de
# acurácia perceptível em documento de escritório.
OCR_DPI = 200
# por+eng: documento brasileiro costuma ter termos em inglês misturados, e o
# tesseract lida melhor com os dois idiomas declarados do que forçando só um.
OCR_LANGS = "por+eng"


class DocumentError(Exception):
    """Base das falhas de documento — o handler mapeia para 4xx."""


class DocumentTooLarge(DocumentError):
    """Excedeu o teto do plano (bytes ou páginas)."""


class UnreadableDocument(DocumentError):
    """Não é um PDF válido, ou está corrompido/protegido."""


class EmptyDocument(DocumentError):
    """PDF válido, mas nenhum texto foi extraído (nem embutido, nem por OCR).

    Vale um erro próprio em vez de seguir com string vazia: mandar prompt sem
    documento pro modelo produziria um JSON inventado, que é bem pior que uma
    falha explícita — o cliente não teria como distinguir "o documento não
    tinha o campo" de "o documento nunca foi lido"."""


def limit_bytes(plan: str | None) -> int:
    return MAX_DOCUMENT_BYTES.get(plan or "", DEFAULT_MAX_DOCUMENT_BYTES)


def max_limit_bytes() -> int:
    """O teto de bytes mais alto entre todos os planos.

    Serve ao caso em que o plano ainda NÃO é conhecido: o handler precisa
    recusar um upload absurdo antes de carregá-lo em memória, e resolver o
    plano custa uma ida ao banco (e pode até religar um pod). Este é o corte
    grosso que nenhum plano supera; o teto exato do plano é aplicado depois,
    por check_size."""
    return max([*MAX_DOCUMENT_BYTES.values(), DEFAULT_MAX_DOCUMENT_BYTES])


def limit_pages(plan: str | None) -> int:
    return MAX_DOCUMENT_PAGES.get(plan or "", DEFAULT_MAX_DOCUMENT_PAGES)


def check_size(size: int, plan: str | None) -> None:
    """Teto de bytes, checado antes de qualquer parsing. Separado de
    extract_text porque o handler chama isto assim que tem o upload em mão —
    não faz sentido abrir um PDF que já vai ser recusado."""
    ceiling = limit_bytes(plan)
    if size > ceiling:
        raise DocumentTooLarge(
            f"documento excede o limite deste plano "
            f"({size // (1024 * 1024)} MB > {ceiling // (1024 * 1024)} MB)"
        )


def limit_image_bytes(plan: str | None) -> int:
    return MAX_IMAGE_BYTES.get(plan or "", DEFAULT_MAX_IMAGE_BYTES)


def max_limit_image_bytes() -> int:
    """Equivalente a max_limit_bytes(), mas pro eixo de imagem — mesmo motivo:
    corte grosso antes de conhecer o plano do cliente."""
    return max([*MAX_IMAGE_BYTES.values(), DEFAULT_MAX_IMAGE_BYTES])


def check_image_size(size: int, plan: str | None) -> None:
    """Teto de bytes de imagem, checado antes de qualquer decode. Mesmo papel
    de check_size, eixo separado porque os tetos por plano são diferentes."""
    ceiling = limit_image_bytes(plan)
    if size > ceiling:
        raise DocumentTooLarge(
            f"imagem excede o limite deste plano "
            f"({size // (1024 * 1024)} MB > {ceiling // (1024 * 1024)} MB)"
        )


def extract_text(pdf_bytes: bytes, plan: str | None) -> tuple[str, int, bool]:
    """Devolve (texto, número de páginas, se usou OCR).

    BLOQUEANTE por design (fitz e pytesseract são síncronos e usam CPU).
    Chamar SEMPRE via asyncio.to_thread — ver o cabeçalho do módulo.

    O OCR é decidido por PÁGINA, não pelo documento: PDF misto (uma capa
    escaneada seguida de páginas digitais) é comum, e rodar OCR nas páginas
    que já têm texto embutido seria desperdiçar CPU e piorar o resultado (o
    texto embutido é sempre mais fiel que o reconhecido)."""
    try:
        import fitz  # pymupdf
    except ImportError as e:  # pragma: no cover - ambiente sem a dependência
        raise DocumentError("suporte a PDF indisponível no servidor") from e

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:
        raise UnreadableDocument("não foi possível ler o PDF (arquivo inválido ou protegido)") from e

    with doc:
        pages = doc.page_count
        # ANTES de qualquer OCR: é o que torna o teto de páginas uma defesa de
        # CPU de verdade. Checar no fim (ou por página) já teria pago o custo.
        ceiling = limit_pages(plan)
        if pages > ceiling:
            raise DocumentTooLarge(
                f"documento tem {pages} páginas, o limite deste plano é {ceiling}"
            )

        parts: list[str] = []
        ocr_used = False
        for page in doc:
            text = page.get_text().strip()
            if text:
                parts.append(text)
                continue
            parts.append(_ocr_page(page))
            ocr_used = True

    full = "\n\n".join(p for p in parts if p).strip()
    if not full:
        raise EmptyDocument(
            "nenhum texto foi extraído do documento (PDF sem texto e ilegível por OCR)"
        )
    return full, pages, ocr_used


def _ocr_page(page) -> str:
    """OCR de uma página sem texto embutido. Falha de OCR numa página não
    aborta o documento: devolve string vazia e deixa as outras páginas
    valerem — um documento de 20 páginas não deve morrer porque uma delas é
    uma imagem ilegível."""
    try:
        import pytesseract
        from PIL import Image

        pix = page.get_pixmap(dpi=OCR_DPI)
        with Image.open(io.BytesIO(pix.tobytes("png"))) as img:
            return pytesseract.image_to_string(img, lang=OCR_LANGS).strip()
    except Exception:
        return ""


def extract_text_from_image(image_bytes: bytes, plan: str | None) -> tuple[str, bool]:
    """Devolve (texto, ocr_used=True sempre — imagem só tem o caminho de OCR).

    BLOQUEANTE por design, mesma disciplina de extract_text: chamar SEMPRE
    via asyncio.to_thread.

    Ao contrário de PDF, aqui não existe "texto embutido": toda imagem passa
    por OCR. `ocr_used` é mantido no retorno só pra manter o mesmo shape de
    tupla que extract_text — no handler ele sempre é True."""
    try:
        import pytesseract
        from PIL import Image, UnidentifiedImageError
    except ImportError as e:  # pragma: no cover - ambiente sem a dependência
        raise DocumentError("suporte a imagem indisponível no servidor") from e

    try:
        img = Image.open(io.BytesIO(image_bytes))
    except (UnidentifiedImageError, OSError) as e:
        raise UnreadableDocument(
            "não foi possível ler a imagem (arquivo inválido ou corrompido)"
        ) from e

    with img:
        if img.format not in SUPPORTED_IMAGE_FORMATS:
            raise UnreadableDocument(
                f"formato de imagem não suportado ({img.format}); use JPEG, PNG ou WEBP"
            )

        # ANTES do decode: Image.open só lê o cabeçalho (width/height vêm de
        # lá, sem decodificar pixel nenhum), então dá pra rejeitar aqui sem
        # pagar o custo de decodificação. Checar DEPOIS (ex.: depois de
        # img.load()) não protegeria nada — um PNG de poucos KB pode
        # descomprimir para uma imagem de centenas de MP ("decompression
        # bomb"), e o decode já teria consumido a CPU/memória proporcionais
        # ao tamanho descomprimido antes da checagem rodar.
        megapixels = img.width * img.height
        if megapixels > MAX_IMAGE_MEGAPIXELS:
            raise DocumentTooLarge(
                f"imagem tem {megapixels / 1_000_000:.1f} MP, "
                f"o limite é {MAX_IMAGE_MEGAPIXELS / 1_000_000:.0f} MP"
            )

        try:
            # força a decodificação agora, já dentro do teto de megapixels —
            # um arquivo truncado só falharia no primeiro acesso a pixel,
            # dentro do OCR, com uma exceção menos clara.
            img.load()
        except OSError as e:
            raise UnreadableDocument(
                "não foi possível ler a imagem (arquivo inválido ou corrompido)"
            ) from e

        try:
            text = pytesseract.image_to_string(img, lang=OCR_LANGS).strip()
        except Exception as e:
            # ao contrário de _ocr_page (onde falha de OCR numa página entre
            # várias não aborta o documento), aqui é a imagem inteira — não há
            # texto de outras páginas pra salvar o resultado, então a falha
            # propaga como erro explícito em vez de virar EmptyDocument muda.
            raise DocumentError("falha ao executar OCR na imagem") from e

    if not text:
        raise EmptyDocument(
            "nenhum texto foi encontrado na imagem (ilegível por OCR)"
        )
    return text, True


# Instrução que acompanha o texto extraído. Explícita sobre "só o JSON" mesmo
# com guided decoding ligado (default do vLLM 0.24, ver migration 0048): a
# gramática garante a FORMA da saída, não que o modelo tenha entendido a
# tarefa — sem a instrução ele preenche o schema com valores plausíveis em vez
# de extrair os reais, e o resultado passa em qualquer validação sendo falso.
#
# Sobre campo ausente: a instrução NÃO pode prometer "use null", porque a
# gramática do guided decoding manda mais que o prompt. Num campo declarado
# {"type": "string"} o null é sintaticamente proibido — o modelo é obrigado a
# emitir alguma string, e vai inventar uma. Quem quer poder receber "não
# achei" tem que declarar o campo como anulável ({"type": ["string","null"]})
# ou deixá-lo fora de `required`; isso está documentado em docs/integracao.md.
# Aqui pedimos o que é possível em qualquer schema: omitir em vez de inventar.
EXTRACTION_PROMPT = (
    "Extraia as informações do documento abaixo e devolva um JSON aderente ao "
    "schema solicitado. Use exclusivamente valores presentes no documento — "
    "nunca invente, adivinhe ou infira um valor que não esteja escrito nele. "
    "Se a informação de um campo não aparecer no documento, omita esse campo "
    "(ou use null, se o schema permitir) em vez de preenchê-lo com um valor "
    "plausível."
)

# Instrução do cliente (campo `user` do multipart), quando houver. Entra ENTRE
# a instrução padrão e o documento, com rótulo próprio: sem a marcação, um
# texto do cliente do tipo "ignore as regras acima" se confundiria com a
# instrução do servidor. Assim ele é claramente contexto adicional da tarefa,
# não um substituto das garantias.
USER_INSTRUCTION_BLOCK = "\n\nContexto adicional informado por quem enviou o documento:\n{user}"

DOCUMENT_BLOCK = "\n\n--- DOCUMENTO ---\n{text}\n--- FIM DO DOCUMENTO ---"


def build_messages(text: str, user_instruction: str | None = None) -> list[dict]:
    """Mensagem `user` da extração: instrução padrão + (contexto do cliente) +
    documento.

    O `user_instruction` COMPÕE, nunca substitui — ao contrário do `system`,
    que troca a configuração da stack. A assimetria é proposital e segue a
    mesma lógica do chat: `system` é configuração (faz sentido trocar), `user`
    é tarefa (soma). Se substituísse, o cliente removeria sem querer o "não
    invente, use null" — que é justamente a garantia que impede o modelo de
    fabricar valor pra preencher o schema."""
    parts = [EXTRACTION_PROMPT]
    if user_instruction and user_instruction.strip():
        parts.append(USER_INSTRUCTION_BLOCK.format(user=user_instruction.strip()))
    parts.append(DOCUMENT_BLOCK.format(text=text))
    return [{"role": "user", "content": "".join(parts)}]
