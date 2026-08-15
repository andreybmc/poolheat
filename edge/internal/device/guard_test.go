package device

import (
	"sync"
	"testing"
	"time"

	"github.com/andreybmc/poolheat/edge/internal/config"
	"github.com/andreybmc/poolheat/edge/internal/state"
)

func TestPollWorkers(t *testing.T) {
	if pollWorkers(0) != 1 || pollWorkers(1) != 1 {
		t.Fatalf("single device must stay serial")
	}
	if pollWorkers(3) != 3 {
		t.Fatalf("small fleet should match n")
	}
	if pollWorkers(20) != 8 {
		t.Fatalf("cap at 8, got %d", pollWorkers(20))
	}
}

func TestDeviceLockSkip(t *testing.T) {
	s := NewStore(nil, nil, nil)
	s.LockDevice("a")
	if s.TryLockDevice("a") {
		t.Fatal("TryLock must fail while UI holds the device")
	}
	if !s.TryLockDevice("b") {
		t.Fatal("other device must stay free")
	}
	s.UnlockDevice("b")
	s.UnlockDevice("a")
	if !s.TryLockDevice("a") {
		t.Fatal("lock must release")
	}
	s.UnlockDevice("a")
}

func TestSnapshotRuntimeConcurrent(t *testing.T) {
	s := NewStore(map[string]state.Runtime{"x": {}}, nil, nil)
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			s.update("x", func(r *state.Runtime) {
				on := i%2 == 0
				r.LastOn = &on
			})
			_ = s.SnapshotRuntime()
			_ = s.Runtime("x")
		}(i)
	}
	wg.Wait()
}

func TestStatusTimeoutShort(t *testing.T) {
	p := config.DefaultPoller()
	if p.StatusTimeout() > 8*time.Second {
		t.Fatalf("background status must stay short, got %s", p.StatusTimeout())
	}
	if p.SetTimeout() < 15*time.Second {
		t.Fatalf("set budget too small: %s", p.SetTimeout())
	}
}
