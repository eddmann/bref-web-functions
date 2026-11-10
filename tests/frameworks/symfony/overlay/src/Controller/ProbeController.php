<?php

namespace App\Controller;

use Symfony\Component\HttpFoundation\JsonResponse;
use Symfony\Component\HttpFoundation\RedirectResponse;
use Symfony\Component\HttpFoundation\Request;
use Symfony\Component\HttpFoundation\Response;
use Symfony\Component\HttpFoundation\StreamedResponse;
use Symfony\Component\HttpKernel\KernelInterface;
use Symfony\Component\Routing\Attribute\Route;
use Symfony\Component\Routing\Generator\UrlGeneratorInterface;

/** Reports what the framework derives from the request, for runtime comparison. */
final class ProbeController
{
    public function __construct(private KernelInterface $kernel, private UrlGeneratorInterface $urls)
    {
    }

    #[Route('/probe/{rest}', name: 'probe', requirements: ['rest' => '.*'], methods: ['GET', 'POST'])]
    public function probe(Request $request, string $rest): JsonResponse
    {
        return new JsonResponse([
            'framework' => 'symfony ' . \Symfony\Component\HttpKernel\Kernel::VERSION,
            'method' => $request->getMethod(),
            'rest' => $rest,
            'client_ip' => $request->getClientIp(),
            'client_ips' => $request->getClientIps(),
            'from_trusted_proxy' => $request->isFromTrustedProxy(),
            'secure' => $request->isSecure(),
            'scheme' => $request->getScheme(),
            'host' => $request->getHost(),
            'port' => $request->getPort(),
            'scheme_and_host' => $request->getSchemeAndHttpHost(),
            'uri' => $request->getUri(),
            'request_uri' => $request->getRequestUri(),
            'path_info' => $request->getPathInfo(),
            'base_url' => $request->getBaseUrl(),
            'query' => $request->query->all(),
            'post' => $request->request->all(),
            'json' => json_decode($request->getContent() ?: 'null', true),
            'content_type_format' => $request->getContentTypeFormat(),
            'files' => array_map(fn ($file) => $file?->getSize(), $request->files->all()),
            'cookies' => $request->cookies->all(),
            'user' => $request->getUser(),
            'absolute_url' => $this->urls->generate('probe', ['rest' => 'x/y'], UrlGeneratorInterface::ABSOLUTE_URL),
            'environment' => $this->kernel->getEnvironment(),
            'cache_dir' => $this->kernel->getCacheDir(),
            'log_dir' => $this->kernel->getLogDir(),
            'project_dir' => $this->kernel->getProjectDir(),
        ]);
    }

    #[Route('/session', name: 'session')]
    public function session(Request $request): JsonResponse
    {
        $session = $request->getSession();
        $session->set('count', $session->get('count', 0) + 1);
        return new JsonResponse(['count' => $session->get('count')]);
    }

    #[Route('/redirect', name: 'redirect')]
    public function redirect(): RedirectResponse
    {
        return new RedirectResponse($this->urls->generate('probe', ['rest' => 'after'], UrlGeneratorInterface::ABSOLUTE_URL));
    }

    #[Route('/error', name: 'error')]
    public function error(): Response
    {
        throw new \RuntimeException('Deliberate framework parity error');
    }

    #[Route('/stream', name: 'stream')]
    public function stream(): StreamedResponse
    {
        return new StreamedResponse(function (): void {
            foreach (['one', 'two', 'three'] as $part) {
                echo $part, "\n";
                flush();
            }
        });
    }
}
