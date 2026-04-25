import subprocess

_OUTPUT_LIMIT = 8 * 1024  # 8 KB


def run_safe(argv: list, timeout: int = 10) -> dict:
    """Execute a command via argv list (never shell=True).

    Returns:
        {ok, exit_code, stdout, stderr}
    stdout/stderr are truncated to _OUTPUT_LIMIT bytes combined.
    """
    if not argv or not isinstance(argv, list):
        return {'ok': False, 'exit_code': -1, 'stdout': '', 'stderr': 'argv must be a non-empty list'}
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        stdout = result.stdout
        stderr = result.stderr
        # truncate oversized output
        if len(stdout) > _OUTPUT_LIMIT:
            stdout = stdout[:_OUTPUT_LIMIT] + '\n...[output truncated]'
        if len(stderr) > _OUTPUT_LIMIT:
            stderr = stderr[:_OUTPUT_LIMIT] + '\n...[output truncated]'
        return {
            'ok': result.returncode == 0,
            'exit_code': result.returncode,
            'stdout': stdout,
            'stderr': stderr,
        }
    except subprocess.TimeoutExpired:
        return {'ok': False, 'exit_code': -1, 'stdout': '', 'stderr': f'command timed out after {timeout}s'}
    except FileNotFoundError:
        return {'ok': False, 'exit_code': -1, 'stdout': '', 'stderr': f'command not found: {argv[0]}'}
    except Exception as e:
        return {'ok': False, 'exit_code': -1, 'stdout': '', 'stderr': str(e)}
