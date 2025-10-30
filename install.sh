#!/usr/bin/env bash
# ModelOps Installer Script
# Installs uv (if needed) and the complete ModelOps suite

set -euo pipefail

# Color codes for beautiful output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
MAGENTA='\033[0;35m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

# Fancy banner
print_banner() {
    printf "${CYAN}╔══════════════════════════════════════════════════════════╗${NC}\n"
    printf "${CYAN}║                                                          ║${NC}\n"
    printf "${CYAN}║     ${BOLD}ModelOps/Calabaria Installer${NC}${CYAN}             ║${NC}\n"
    printf "${CYAN}║                                                          ║${NC}\n"
    printf "${CYAN}╚══════════════════════════════════════════════════════════╝${NC}\n"
}

# Print colored messages
info() {
    echo -e "${BLUE}ℹ${NC} $1"
}

success() {
    echo -e "${GREEN}✓${NC} $1"
}

warning() {
    echo -e "${YELLOW}⚠${NC} $1"
}

error() {
    echo -e "${RED}✗${NC} $1"
}

# Check if command exists
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# Detect the shell
detect_shell() {
    if [ -n "${SHELL:-}" ]; then
        case "$SHELL" in
            */bash)
                echo "bash"
                ;;
            */zsh)
                echo "zsh"
                ;;
            */fish)
                echo "fish"
                ;;
            *)
                echo "bash"  # Default fallback
                ;;
        esac
    else
        echo "bash"
    fi
}

# Get shell config file
get_shell_config() {
    local shell_name="$1"
    case "$shell_name" in
        bash)
            if [ -f "$HOME/.bashrc" ]; then
                echo "$HOME/.bashrc"
            elif [ -f "$HOME/.bash_profile" ]; then
                echo "$HOME/.bash_profile"
            else
                echo "$HOME/.profile"
            fi
            ;;
        zsh)
            if [ -f "$HOME/.zshrc" ]; then
                echo "$HOME/.zshrc"
            else
                echo "$HOME/.zprofile"
            fi
            ;;
        fish)
            echo "$HOME/.config/fish/config.fish"
            ;;
        *)
            echo "$HOME/.profile"
            ;;
    esac
}

# Check if directory is in PATH
is_in_path() {
    local dir="$1"
    case ":$PATH:" in
        *":$dir:"*)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

# Install uv if not present
install_uv() {
    if command_exists uv; then
        success "uv is already installed ($(uv --version))"
        return 0
    fi

    info "Installing uv package manager..."

    # Download and install uv
    if command_exists curl; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
    elif command_exists wget; then
        wget -qO- https://astral.sh/uv/install.sh | sh
    else
        error "Neither curl nor wget found. Please install one of them first."
        exit 1
    fi

    # Source the uv env file to get it in current session
    if [ -f "$HOME/.local/bin/env" ]; then
        source "$HOME/.local/bin/env"
    fi

    if command_exists uv; then
        success "uv installed successfully!"
    else
        warning "uv installed but not yet in PATH. Will be available after PATH configuration."
    fi
}

# Install Pulumi CLI if not present
install_pulumi() {
    if command_exists pulumi; then
        success "Pulumi is already installed ($(pulumi version))"
        return 0
    fi

    info "Installing Pulumi CLI (required for infrastructure management)..."

    # Detect OS
    local os_type=""
    if [[ "$OSTYPE" == "linux-gnu"* ]]; then
        os_type="linux"
    elif [[ "$OSTYPE" == "darwin"* ]]; then
        os_type="darwin"
    elif [[ "$OSTYPE" == "cygwin" ]] || [[ "$OSTYPE" == "msys" ]] || [[ "$OSTYPE" == "win32" ]]; then
        os_type="windows"
    else
        os_type="unknown"
    fi

    # Try to install based on available package managers
    if [[ "$os_type" == "darwin" ]] && command_exists brew; then
        info "Installing Pulumi using Homebrew..."
        brew install pulumi
    elif [[ "$os_type" == "windows" ]] && command_exists choco; then
        info "Installing Pulumi using Chocolatey..."
        choco install pulumi
    else
        # Use the universal installer script
        info "Installing Pulumi using official installer..."
        if command_exists curl; then
            curl -fsSL https://get.pulumi.com | sh
        elif command_exists wget; then
            wget -qO- https://get.pulumi.com | sh
        else
            error "Neither curl nor wget found. Please install one of them first."
            exit 1
        fi

        # Add Pulumi to PATH for current session
        export PATH="$HOME/.pulumi/bin:$PATH"
    fi

    # Verify installation
    if command_exists pulumi; then
        success "Pulumi CLI installed successfully!"

        # Configure Pulumi for local backend (development mode)
        info "Configuring Pulumi for local development..."
        pulumi login --local >/dev/null 2>&1 || true
        success "Pulumi configured for local backend"
    else
        warning "Pulumi installed but not yet in PATH. Will be available after PATH configuration."
    fi
}

# Install ModelOps suite
install_modelops() {
    info "Installing ModelOps suite with all components..."
    echo -e "${CYAN}  • mops${NC} - Infrastructure management"
    echo -e "${CYAN}  • modelops-bundle${NC} - Bundle packaging"
    echo -e "${CYAN}  • cb${NC} - Calabaria experiment design"
    echo ""

    # Try to install with full extras, specifying Python 3.12+
    if uv tool install --python ">=3.12" "modelops[full]@git+https://github.com/institutefordiseasemodeling/modelops.git" 2>/dev/null; then
        success "ModelOps suite installed successfully!"
    else
        # If uv is not in PATH yet, try with full path
        if [ -x "$HOME/.local/bin/uv" ]; then
            "$HOME/.local/bin/uv" tool install --python ">=3.12" "modelops[full]@git+https://github.com/institutefordiseasemodeling/modelops.git"
            if [ $? -eq 0 ]; then
                success "ModelOps suite installed successfully!"
            else
                error "Failed to install ModelOps. This requires Python 3.12 or later."
                info "uv can automatically install Python 3.12 for you."
                info "If installation failed, try: uv python install 3.12"
                exit 1
            fi
        else
            error "Failed to install ModelOps. Please check your network connection."
            exit 1
        fi
    fi
}

# Configure PATH
configure_path() {
    local bin_dir="$HOME/.local/bin"
    local pulumi_dir="$HOME/.pulumi/bin"
    local shell_name=$(detect_shell)
    local shell_config=$(get_shell_config "$shell_name")
    local needs_update=false

    # Check if both directories are in PATH
    if ! is_in_path "$bin_dir"; then
        needs_update=true
    fi

    # Check if Pulumi was installed via the script (not brew/choco)
    if [ -d "$pulumi_dir" ] && ! is_in_path "$pulumi_dir"; then
        needs_update=true
    fi

    if [ "$needs_update" = false ]; then
        success "PATH already configured correctly"
        return 0
    fi

    echo ""
    warning "PATH configuration needed"
    info "ModelOps tools are installed in: ${BOLD}$bin_dir${NC}"
    if [ -d "$pulumi_dir" ]; then
        info "Pulumi CLI is installed in: ${BOLD}$pulumi_dir${NC}"
    fi
    echo ""
    echo "To use ModelOps commands, you need to add these directories to your PATH."
    echo ""

    # Generate the appropriate command for their shell
    local path_cmd=""
    local paths_to_add=""

    # Build the paths we need to add
    if [ -d "$pulumi_dir" ] && ! is_in_path "$pulumi_dir"; then
        paths_to_add="$pulumi_dir"
    fi
    if ! is_in_path "$bin_dir"; then
        if [ -n "$paths_to_add" ]; then
            paths_to_add="$bin_dir:$paths_to_add"
        else
            paths_to_add="$bin_dir"
        fi
    fi

    case "$shell_name" in
        fish)
            path_cmd="set -U fish_user_paths $paths_to_add \$fish_user_paths"
            ;;
        *)
            path_cmd="export PATH=\"$paths_to_add:\$PATH\""
            ;;
    esac

    echo -e "${BOLD}Option 1: Automatic configuration${NC}"
    echo "Add the following line to $shell_config:"
    echo ""
    echo -e "    ${GREEN}$path_cmd${NC}"
    echo ""

    # Offer to add it automatically
    read -p "Would you like to add this automatically? [y/N] " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        # Check if it's already there
        if ! grep -q "$bin_dir" "$shell_config" 2>/dev/null; then
            echo "" >> "$shell_config"
            echo "# Added by ModelOps installer" >> "$shell_config"
            echo "$path_cmd" >> "$shell_config"
            success "PATH configuration added to $shell_config"
            echo ""
            info "Please run: ${BOLD}source $shell_config${NC}"
            info "Or start a new terminal session"
        else
            info "PATH configuration already exists in $shell_config"
        fi
    else
        echo ""
        echo -e "${BOLD}Option 2: Manual configuration${NC}"
        echo "Add this line to $shell_config yourself:"
        echo ""
        echo -e "    ${GREEN}$path_cmd${NC}"
        echo ""
        echo "Then reload your shell configuration:"
        echo -e "    ${GREEN}source $shell_config${NC}"
    fi
}

# Verify installation
verify_installation() {
    echo ""
    info "Verifying installation..."

    local tools_found=true

    # Check if mops is installed (the main entry point)
    if [ -x "$HOME/.local/bin/mops" ]; then
        echo -e "  ${GREEN}✓${NC} mops installed"
        # Check if bundle and cb commands are available through mops
        if "$HOME/.local/bin/mops" bundle --help >/dev/null 2>&1; then
            echo -e "  ${GREEN}✓${NC} mops bundle available"
        else
            echo -e "  ${YELLOW}⚠${NC} mops bundle not available (modelops-bundle may not be installed)"
            tools_found=false
        fi
    else
        echo -e "  ${RED}✗${NC} mops not found"
        tools_found=false
    fi

    if [ "$tools_found" = true ]; then
        echo ""
        success "All ModelOps tools installed successfully!"
        echo ""
        printf "${BOLD}Quick Start:${NC}\n"
        echo "  1. Configure your Azure credentials:"
        printf "     ${CYAN}az login${NC}\n"
        echo ""
        echo "  2. Initialize ModelOps configuration:"
        printf "     ${CYAN}mops init${NC}\n"
        echo ""
        echo "  3. Deploy infrastructure:"
        printf "     ${CYAN}mops infra up${NC}\n"
        echo ""
        echo "  4. Create a new project:"
        printf "     ${CYAN}mkdir my-project && cd my-project${NC}\n"
        printf "     ${CYAN}mops bundle init .${NC}\n"
        echo ""
        echo "For more information: https://github.com/institutefordiseasemodeling/modelops"
    else
        warning "Some tools were not installed correctly."
        echo "Please check the installation and try again."
    fi
}

# Main installation flow
main() {
    print_banner

    info "Starting ModelOps installation..."
    echo ""

    # Step 1: Install uv
    install_uv
    echo ""

    # Step 2: Install Pulumi CLI
    install_pulumi
    echo ""

    # Step 3: Install ModelOps
    install_modelops
    echo ""

    # Step 4: Configure PATH
    configure_path
    echo ""

    # Step 5: Verify
    verify_installation
}

# Run main function
main
