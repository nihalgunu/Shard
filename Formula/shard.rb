class Shard < Formula
  include Language::Python::Virtualenv

  desc "TDD-driven, parallelized AI coding orchestrator"
  homepage "https://github.com/nihalgunu/Shard"
  url "https://github.com/nihalgunu/Shard/archive/refs/tags/v1.0.2.tar.gz"
  sha256 "cecae7d41fe52adf768285285ef9d9cd05d43b205f8259d21d1801cdd2b6b603"
  license "Apache-2.0"

  depends_on "python@3.11"

  def install
    virtualenv_install_with_resources
  end

  test do
    system "#{bin}/shard", "--version"
  end
end
