import digest
import digest_policy
import recent_memory_runtime


recent_memory_runtime.install(digest)
digest_policy.install(digest)


if __name__ == "__main__":
    digest.main()
