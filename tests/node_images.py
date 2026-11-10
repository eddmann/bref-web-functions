"""The managed Node 24 images every archive test runs in, pinned per architecture."""

NODE_IMAGES = {
    "x86_64": "public.ecr.aws/lambda/nodejs:24@sha256:d03ccef4a3be84ef9891b371f526198209d010c0102afd87f4760d9075bcceb7",
    "aarch64": "public.ecr.aws/lambda/nodejs:24@sha256:657dcbd936861638a626cdaab538238f40cd346d7e239ae8c4c487dfd4bf31f2",
}
