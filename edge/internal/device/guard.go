package device

import (
	"sync"

	"github.com/andreybmc/poolheat/edge/internal/state"
)

func (s *Store) initGuards() {
	if s.devMu == nil {
		s.devMu = map[string]*sync.Mutex{}
	}
	if s.DesiredTouched == nil {
		s.DesiredTouched = map[string]bool{}
	}
	if s.EnforceTS == nil {
		s.EnforceTS = map[string]float64{}
	}
}

func (s *Store) mutexFor(id string) *sync.Mutex {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.initGuards()
	m := s.devMu[id]
	if m == nil {
		m = &sync.Mutex{}
		s.devMu[id] = m
	}
	return m
}

// LockDevice serializes Control() for one device (UI set vs background poll).
func (s *Store) LockDevice(id string) {
	if id == "" {
		return
	}
	s.mutexFor(id).Lock()
}

// UnlockDevice releases LockDevice.
func (s *Store) UnlockDevice(id string) {
	if id == "" {
		return
	}
	s.mutexFor(id).Unlock()
}

// TryLockDevice is for background poll/hold: skip this tick if UI holds the device.
func (s *Store) TryLockDevice(id string) bool {
	if id == "" {
		return true
	}
	return s.mutexFor(id).TryLock()
}

// Runtime is the thread-safe snapshot of one device.
func (s *Store) Runtime(id string) state.Runtime {
	return s.getRT(id)
}

func (s *Store) getRTLocked(id string) state.Runtime {
	if r, ok := s.ByID[id]; ok {
		return r
	}
	return state.Runtime{}
}

func (s *Store) touchDesired(id string) {
	s.mu.Lock()
	s.initGuards()
	s.DesiredTouched[id] = true
	s.mu.Unlock()
}

func (s *Store) desiredTouched(id string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.DesiredTouched[id]
}

func (s *Store) setSyncTS(id string, ts float64) {
	s.mu.Lock()
	if s.SyncTS == nil {
		s.SyncTS = map[string]float64{}
	}
	s.SyncTS[id] = ts
	s.mu.Unlock()
}

func (s *Store) getSyncTS(id string) float64 {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.SyncTS[id]
}

func (s *Store) setEnforceTS(id string, ts float64) {
	s.mu.Lock()
	if s.EnforceTS == nil {
		s.EnforceTS = map[string]float64{}
	}
	s.EnforceTS[id] = ts
	s.mu.Unlock()
}

func (s *Store) getEnforceTS(id string) float64 {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.EnforceTS[id]
}

func (s *Store) deadlineOf(id string) (float64, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	v, ok := s.Deadlines[id]
	return v, ok
}

func (s *Store) setDeadline(id string, ts float64) {
	s.mu.Lock()
	if s.Deadlines == nil {
		s.Deadlines = map[string]float64{}
	}
	s.Deadlines[id] = ts
	s.mu.Unlock()
}

func (s *Store) clearDeadline(id string) {
	s.mu.Lock()
	delete(s.Deadlines, id)
	s.mu.Unlock()
}

// SnapshotRuntime copies ByID for atomic JSON save (UI cmd may mutate concurrently).
func (s *Store) SnapshotRuntime() map[string]state.Runtime {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make(map[string]state.Runtime, len(s.ByID))
	for k, v := range s.ByID {
		out[k] = v
	}
	return out
}

// SnapshotDeadlines copies maps for atomic JSON save.
func (s *Store) SnapshotDeadlines() (deadlines, syncTS map[string]float64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	deadlines = make(map[string]float64, len(s.Deadlines))
	for k, v := range s.Deadlines {
		deadlines[k] = v
	}
	syncTS = make(map[string]float64, len(s.SyncTS))
	for k, v := range s.SyncTS {
		syncTS[k] = v
	}
	return deadlines, syncTS
}

func pollWorkers(n int) int {
	if n <= 1 {
		return 1
	}
	if n > 8 {
		return 8
	}
	return n
}
