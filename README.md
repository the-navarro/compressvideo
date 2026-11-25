# Compressor de Vídeo

Interface gráfica simples sobre o FFmpeg para reduzir o tamanho de vídeos (ideal para gravações de reuniões).

- Os vídeos originais **nunca são alterados**: os comprimidos são salvos como `nome_comprimido.mp4`, por padrão numa subpasta `comprimidos` ao lado de cada original.
- Compressão em lote, com barra de progresso, tempo restante e relatório de redução por arquivo.
- Perfis prontos (Reunião recomendado, Reunião máxima compressão, Alta qualidade) e ajustes manuais de codec, qualidade (CRF), velocidade, resolução, FPS e áudio.
- Suporte a H.264 e H.265, além de NVENC (NVIDIA) quando disponível.

## Requisitos

- Python 3.8+ com tkinter
- FFmpeg (`ffmpeg` e `ffprobe` no PATH, ou na mesma pasta do script)

Instalando o FFmpeg:

| Sistema | Comando |
|---|---|
| Windows | `winget install Gyan.FFmpeg` |
| macOS | `brew install ffmpeg` |
| Linux | `sudo apt install ffmpeg` |

## Uso

```bash
python compressor_video.py
```

1. Adicione vídeos ou uma pasta.
2. Escolha um perfil (ou personalize).
3. Escolha onde salvar e clique em **Comprimir**.

## Versão web (instalável)

A pasta [`docs/`](docs/) tem uma versão web do compressor, que roda no navegador (Chrome/Edge). Os vídeos são processados no seu computador e nunca são enviados para a internet.

- Uso rápido: abra `docs/index.html` no Chrome ou Edge.
- Instalar como aplicativo: o navegador só oferece "Instalar" em endereços https. Com o GitHub Pages ligado (Settings → Pages → Branch `main`, pasta `/docs`), abra o endereço publicado e clique em "Instalar aplicativo". Depois de instalado, funciona offline.

## Licença

MIT
