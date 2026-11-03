"""Controle de acesso: contas com login e senha, perfis e bloqueio por tentativas.

As senhas nunca são gravadas: guarda-se apenas o hash scrypt com sal aleatório.
A pessoa (tabela `usuario`, a mesma que assina as decisões) e a credencial
(tabela `cf_acesso`) ficam separadas, para que as decisões já registradas por
um nome continuem ligadas à mesma pessoa quando ela recebe um login.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from validacao.conferencia import modelos as cf
from validacao.modelos import Usuario

PERFIS = ("ANALISTA", "REVISOR", "ADMINISTRADOR")
DESCRICAO_PERFIL = {
    "ANALISTA": "Consulta os dados e registra decisões.",
    "REVISOR": "Igual ao analista; identifica quem revisa as decisões.",
    "ADMINISTRADOR": "Além disso, cria contas, redefine senhas e desativa acessos.",
}
LOGIN_ADMIN = "admin"
# Hash da senha inicial do administrador (a senha foi entregue ao responsável e não está no código).
# Pode ser substituída na primeira subida pela variável CONFERENCIA_ADMIN_SENHA.
HASH_ADMIN_INICIAL = (
    "scrypt$32768$8$1$ngJXfr8S8lL8aywyqtiKdQ==$"
    "1nzN4BWcWNcQmErKcD1dnMFk9zyMtHQlTdn1jTpuaLI="
)
MAX_FALHAS = 5
BLOQUEIO = timedelta(minutes=15)
TAMANHO_MINIMO = 12
_PADRAO_LOGIN = re.compile(r"^[a-z0-9][a-z0-9._-]{2,59}$")
_ALFABETO = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
_SIMBOLOS = "@#%+=!?"


class AcessoRecusado(ValueError):
    pass


# ------------------------------------------------------------------ senhas

def gerar_hash(senha: str, n: int = 2**15, r: int = 8, p: int = 1) -> str:
    sal = os.urandom(16)
    chave = hashlib.scrypt(senha.encode("utf-8"), salt=sal, n=n, r=r, p=p, maxmem=128 * 1024 * 1024, dklen=32)
    return f"scrypt${n}${r}${p}${base64.b64encode(sal).decode()}${base64.b64encode(chave).decode()}"


def conferir_senha(senha: str, guardado: str) -> bool:
    try:
        algoritmo, n, r, p, sal, chave = guardado.split("$")
        if algoritmo != "scrypt":
            return False
        calculada = hashlib.scrypt(senha.encode("utf-8"), salt=base64.b64decode(sal), n=int(n), r=int(r), p=int(p),
                                   maxmem=128 * 1024 * 1024, dklen=32)
        return hmac.compare_digest(calculada, base64.b64decode(chave))
    except (ValueError, TypeError):
        return False


def problema_da_senha(senha: str, login: str = "", nome: str = "") -> str | None:
    """Devolve o motivo pelo qual a senha é fraca, ou None se é aceitável."""
    if len(senha) < TAMANHO_MINIMO:
        return f"A senha precisa ter pelo menos {TAMANHO_MINIMO} caracteres."
    classes = sum((
        any(c.islower() for c in senha), any(c.isupper() for c in senha),
        any(c.isdigit() for c in senha), any(not c.isalnum() for c in senha),
    ))
    if classes < 3:
        return "Use ao menos três destes tipos: minúsculas, maiúsculas, números e símbolos."
    baixa = senha.lower()
    for parte in [login.lower(), *nome.lower().split()]:
        if len(parte) >= 4 and parte in baixa:
            return "A senha não pode conter o login ou o nome."
    if len(set(senha)) < 6:
        return "A senha repete caracteres demais."
    return None


def gerar_senha(tamanho: int = 20) -> str:
    while True:
        senha = "".join(secrets.choice(_ALFABETO + _SIMBOLOS) for _ in range(tamanho))
        if (sum(c in _SIMBOLOS for c in senha) >= 2 and senha[0].isalnum() and senha[-1].isalnum()
                and problema_da_senha(senha) is None):
            return senha


def normalizar_login(login: str) -> str:
    return (login or "").strip().lower()


# ------------------------------------------------------------------ contas

def acesso_de(session: Session, usuario_id: int) -> cf.Acesso | None:
    return session.get(cf.Acesso, usuario_id)


def contas(session: Session) -> list[tuple[Usuario, cf.Acesso | None]]:
    """Todas as pessoas, com ou sem login (as sem login vêm de decisões anteriores ao controle de acesso)."""
    linhas = session.execute(
        select(Usuario, cf.Acesso).outerjoin(cf.Acesso, cf.Acesso.usuario_id == Usuario.id).order_by(Usuario.nome)
    ).all()
    return [(u, a) for u, a in linhas]


def _administradores_ativos(session: Session) -> int:
    return session.scalar(
        select(func.count()).select_from(cf.Acesso).join(Usuario, Usuario.id == cf.Acesso.usuario_id)
        .where(cf.Acesso.ativo.is_(True), Usuario.perfil == "ADMINISTRADOR")
    ) or 0


def garantir_admin(session: Session) -> bool:
    """Cria a conta `admin` se ainda não há nenhuma conta. Devolve True se criou."""
    if session.scalar(select(func.count()).select_from(cf.Acesso)):
        return False
    senha_env = os.environ.get("CONFERENCIA_ADMIN_SENHA", "")
    usuario = session.scalar(select(Usuario).where(Usuario.nome == "Administrador"))
    if usuario is None:
        usuario = Usuario(nome="Administrador", perfil="ADMINISTRADOR")
        session.add(usuario)
        session.flush()
    usuario.perfil = "ADMINISTRADOR"
    session.add(cf.Acesso(
        usuario_id=usuario.id, login=LOGIN_ADMIN,
        senha_hash=gerar_hash(senha_env) if senha_env else HASH_ADMIN_INICIAL,
        ativo=True, trocar_senha=False, criado_em=datetime.now(), criado_por="instalação",
    ))
    session.commit()
    return True


def criar_conta(session: Session, login: str, nome: str, perfil: str, senha: str, criado_por: str,
                trocar_senha: bool = True) -> Usuario:
    login, nome = normalizar_login(login), (nome or "").strip()
    if not _PADRAO_LOGIN.match(login):
        raise AcessoRecusado("Login inválido: use de 3 a 60 caracteres entre letras minúsculas, números, ponto, hífen e sublinhado.")
    if not nome:
        raise AcessoRecusado("Informe o nome da pessoa (é o nome que assina as decisões).")
    if perfil not in PERFIS:
        raise AcessoRecusado("Perfil inválido.")
    if session.scalar(select(cf.Acesso).where(cf.Acesso.login == login)):
        raise AcessoRecusado(f"Já existe uma conta com o login {login}.")
    motivo = problema_da_senha(senha, login, nome)
    if motivo:
        raise AcessoRecusado(motivo)
    usuario = session.scalar(select(Usuario).where(Usuario.nome == nome))
    if usuario is not None and acesso_de(session, usuario.id) is not None:
        raise AcessoRecusado(f"{nome} já tem login ({acesso_de(session, usuario.id).login}).")
    if usuario is None:  # pessoa nova; se o nome já existia (decisões antigas), o histórico fica ligado à conta
        usuario = Usuario(nome=nome, perfil=perfil)
        session.add(usuario)
        session.flush()
    usuario.perfil = perfil
    session.add(cf.Acesso(usuario_id=usuario.id, login=login, senha_hash=gerar_hash(senha), ativo=True,
                          trocar_senha=trocar_senha, criado_em=datetime.now(), criado_por=criado_por))
    session.commit()
    return usuario


def definir_senha(session: Session, usuario_id: int, senha: str, trocar_senha: bool) -> None:
    acesso = acesso_de(session, usuario_id)
    usuario = session.get(Usuario, usuario_id)
    if acesso is None or usuario is None:
        raise AcessoRecusado("Conta não encontrada.")
    motivo = problema_da_senha(senha, acesso.login, usuario.nome)
    if motivo:
        raise AcessoRecusado(motivo)
    if conferir_senha(senha, acesso.senha_hash):
        raise AcessoRecusado("A nova senha deve ser diferente da atual.")
    acesso.senha_hash = gerar_hash(senha)
    acesso.trocar_senha = trocar_senha
    acesso.falhas = 0
    acesso.bloqueado_ate = None
    acesso.versao_sessao = (acesso.versao_sessao or 0) + 1  # encerra as sessões abertas com a senha antiga
    session.commit()


def alterar_conta(session: Session, usuario_id: int, autor_id: int, perfil: str | None = None,
                  ativo: bool | None = None) -> None:
    acesso = acesso_de(session, usuario_id)
    usuario = session.get(Usuario, usuario_id)
    if acesso is None or usuario is None:
        raise AcessoRecusado("Conta não encontrada.")
    era_admin_ativo = acesso.ativo and usuario.perfil == "ADMINISTRADOR"
    perde_admin = (perfil is not None and perfil != "ADMINISTRADOR") or ativo is False
    if era_admin_ativo and perde_admin and _administradores_ativos(session) <= 1:
        raise AcessoRecusado("É preciso manter ao menos um administrador ativo.")
    if usuario_id == autor_id and ativo is False:
        raise AcessoRecusado("Você não pode desativar a própria conta.")
    if perfil is not None:
        if perfil not in PERFIS:
            raise AcessoRecusado("Perfil inválido.")
        usuario.perfil = perfil
    if ativo is not None:
        acesso.ativo = ativo
        if not ativo:
            acesso.versao_sessao = (acesso.versao_sessao or 0) + 1
        else:
            acesso.falhas, acesso.bloqueado_ate = 0, None
    session.commit()


def autenticar(session: Session, login: str, senha: str) -> tuple[Usuario, cf.Acesso]:
    """Confere login e senha. Mensagem genérica para não revelar se o login existe."""
    login = normalizar_login(login)
    acesso = session.scalar(select(cf.Acesso).where(cf.Acesso.login == login)) if login else None
    agora = datetime.now()
    if acesso is None:
        conferir_senha(senha, HASH_ADMIN_INICIAL)  # mesmo tempo de resposta de um login existente
        raise AcessoRecusado("Login ou senha incorretos.")
    if acesso.bloqueado_ate and acesso.bloqueado_ate > agora:
        minutos = max(1, int((acesso.bloqueado_ate - agora).total_seconds() // 60) + 1)
        raise AcessoRecusado(f"Conta bloqueada por excesso de tentativas. Tente de novo em {minutos} min.")
    if not conferir_senha(senha, acesso.senha_hash):
        acesso.falhas = (acesso.falhas or 0) + 1
        if acesso.falhas >= MAX_FALHAS:
            acesso.bloqueado_ate, acesso.falhas = agora + BLOQUEIO, 0
            session.commit()
            raise AcessoRecusado(f"Conta bloqueada por {int(BLOQUEIO.total_seconds() // 60)} min após {MAX_FALHAS} tentativas erradas.")
        session.commit()
        raise AcessoRecusado("Login ou senha incorretos.")
    if not acesso.ativo:
        raise AcessoRecusado("Esta conta está desativada. Fale com o administrador.")
    acesso.falhas, acesso.bloqueado_ate, acesso.ultimo_acesso = 0, None, agora
    session.commit()
    return session.get(Usuario, acesso.usuario_id), acesso
